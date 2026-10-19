"""
Agent Asset Adapter（A3 / §5.2.6）：只读同步运行态本体 / Skill manifest / tool schema / script 清单。

职责：
- 从 Agent 推理服务获取运行态资产摘要
- 解析为结构化 AssetCatalog
- 不在热路径上解压/解析 zip；仅同步 hash + 结构化摘要

资产类型（asset_type）：
- ontology: Agent 本体定义（action / node / edge）
- skill_manifest: Skill 配置清单（script / action / tool 列表）
- tool_schema: 工具 OpenAPI / JSON Schema
- script_list: 脚本 / action 文件清单
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from logger import log


# ── 数据结构 ────────────────────────────────────────────────────


@dataclass
class OperationCandidate:
    """从资产解析出的可绑定操作候选。"""

    operation_id: str  # 稳定业务操作 ID，如 voice.customer_feedback.parse
    display_name: str
    source_type: str  # script | tool | action | skill
    source_path: Optional[str] = None  # 文件路径 / 函数路径
    match_hints: Dict[str, Any] = field(default_factory=dict)
    # match_hints 示例：
    #   {"script_name": "客户声音结构化解析.py", "tool_name": "execute_script"}
    #   {"agent_step_kind": "tool", "agent_tool_name": "web_search"}


@dataclass
class AssetCatalog:
    """一个 Agent 的全部资产目录。"""

    logical_agent_id: str
    agent_version: Optional[str] = None
    operations: List[OperationCandidate] = field(default_factory=list)
    raw_assets: Dict[str, Any] = field(default_factory=dict)
    # raw_assets key = asset_type, value = 解析后的结构化内容


# ── 解析器 ──────────────────────────────────────────────────────


def _hash_payload(payload: Any) -> str:
    """计算资产内容的 SHA-256 摘要。"""
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_ontology(ontology_data: Dict[str, Any]) -> List[OperationCandidate]:
    """解析本体 JSON，提取 action 列表为操作候选。

    预期结构（灵活适配）：
    {
      "actions": [{"name": "...", "display_name": "...", ...}],
      "nodes": [...],
    }
    也支持扁平列表：[{"name": "...", "type": "action", ...}]
    """
    candidates: List[OperationCandidate] = []

    actions = ontology_data.get("actions", [])
    if isinstance(actions, list):
        for act in actions:
            name = act.get("name", "") or act.get("id", "")
            if not name:
                continue
            op_id = _normalize_operation_id(name)
            candidates.append(
                OperationCandidate(
                    operation_id=op_id,
                    display_name=act.get("display_name") or act.get("name") or op_id,
                    source_type="action",
                    source_path=act.get("path") or act.get("script_path"),
                    match_hints={
                        "agent_step_id": act.get("step_id", name),
                        "agent_step_kind": "action",
                    },
                )
            )

    # 扁平列表
    items = ontology_data.get("items", [])
    if isinstance(items, list) and not candidates:
        for item in items:
            name = item.get("name", "") or item.get("id", "")
            if not name or item.get("type") != "action":
                continue
            op_id = _normalize_operation_id(name)
            candidates.append(
                OperationCandidate(
                    operation_id=op_id,
                    display_name=item.get("display_name") or name,
                    source_type="action",
                    match_hints={"agent_step_id": name},
                )
            )

    return candidates


def parse_skill_manifest(manifest_data: Dict[str, Any]) -> List[OperationCandidate]:
    """解析 Skill manifest，提取 script / tool / action 清单。

    预期结构：
    {
      "scripts": [{"name": "...", "path": "..."}],
      "tools": [{"name": "...", "schema": {...}}],
      "actions": [{"name": "...", "skill_name": "..."}]
    }
    """
    candidates: List[OperationCandidate] = []

    for script in manifest_data.get("scripts", []):
        name = script.get("name", "")
        path = script.get("path", "")
        if not name and not path:
            continue
        label = name or path.rsplit("/", 1)[-1]
        op_id = _normalize_operation_id(label)
        hints: Dict[str, Any] = {"script_name": label}
        if path:
            hints["agent_script_path"] = path
        candidates.append(
            OperationCandidate(
                operation_id=op_id,
                display_name=script.get("display_name") or label,
                source_type="script",
                source_path=path,
                match_hints=hints,
            )
        )

    for tool in manifest_data.get("tools", []):
        name = tool.get("name", "")
        if not name:
            continue
        op_id = _normalize_operation_id(name)
        hints: Dict[str, Any] = {"agent_tool_name": name, "agent_step_kind": "tool"}
        candidates.append(
            OperationCandidate(
                operation_id=op_id,
                display_name=tool.get("display_name") or name,
                source_type="tool",
                source_path=tool.get("path"),
                match_hints=hints,
            )
        )

    for action in manifest_data.get("actions", []):
        name = action.get("name", "")
        if not name:
            continue
        op_id = _normalize_operation_id(name)
        hints: Dict[str, Any] = {"agent_step_id": name, "agent_step_kind": "action"}
        skill = action.get("skill_name", "")
        if skill:
            hints["agent_script_path"] = skill
        candidates.append(
            OperationCandidate(
                operation_id=op_id,
                display_name=action.get("display_name") or name,
                source_type="action",
                source_path=skill or action.get("path"),
                match_hints=hints,
            )
        )

    return candidates


def parse_tool_schema(schema_data: Dict[str, Any]) -> List[OperationCandidate]:
    """解析 OpenAPI / JSON Schema 工具清单。

    预期结构（OpenAPI paths 子集）：
    {
      "paths": {"/tool/name": {"post": {"operationId": "...", ...}}}
    }
    或扁平列表：
    [{"name": "...", "description": "..."}]
    """
    candidates: List[OperationCandidate] = []

    # OpenAPI paths
    paths = schema_data.get("paths", {})
    if isinstance(paths, dict):
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, spec in methods.items():
                op_id_raw = spec.get("operationId", "") or path.strip("/").replace("/", ".")
                if not op_id_raw:
                    continue
                op_id = _normalize_operation_id(op_id_raw)
                candidates.append(
                    OperationCandidate(
                        operation_id=op_id,
                        display_name=spec.get("summary") or op_id,
                        source_type="tool",
                        source_path=path,
                        match_hints={"agent_tool_name": op_id_raw, "agent_step_kind": "tool"},
                    )
                )

    # 扁平列表
    tools = schema_data.get("tools", [])
    if isinstance(tools, list) and not candidates:
        for tool in tools:
            name = tool.get("name", "")
            if not name:
                continue
            op_id = _normalize_operation_id(name)
            candidates.append(
                OperationCandidate(
                    operation_id=op_id,
                    display_name=tool.get("description") or name,
                    source_type="tool",
                    match_hints={"agent_tool_name": name, "agent_step_kind": "tool"},
                )
            )

    return candidates


def parse_script_list(scripts_data: List[Dict[str, Any]]) -> List[OperationCandidate]:
    """解析脚本 / action 文件清单。

    预期结构：[{"name": "...", "path": "..."}]
    """
    candidates: List[OperationCandidate] = []
    for item in scripts_data:
        name = item.get("name", "")
        path = item.get("path", "")
        if not name and not path:
            continue
        label = name or path.rsplit("/", 1)[-1]
        op_id = _normalize_operation_id(label)
        hints: Dict[str, Any] = {"script_name": label}
        if path:
            hints["agent_script_path"] = path
        candidates.append(
            OperationCandidate(
                operation_id=op_id,
                display_name=item.get("display_name") or label,
                source_type="script",
                source_path=path,
                match_hints=hints,
            )
        )
    return candidates


# ── 主入口 ──────────────────────────────────────────────────────


async def build_asset_catalog(
    logical_agent_id: str,
    *,
    agent_version: Optional[str] = None,
    ontology: Optional[Dict[str, Any]] = None,
    skill_manifest: Optional[Dict[str, Any]] = None,
    tool_schema: Optional[Dict[str, Any]] = None,
    script_list: Optional[List[Dict[str, Any]]] = None,
) -> AssetCatalog:
    """从各类资产数据构建 AssetCatalog。

    调用方负责获取原始资产（HTTP / 文件 / zip 解压等），
    本函数只做结构化解析和去重。
    """
    catalog = AssetCatalog(
        logical_agent_id=logical_agent_id,
        agent_version=agent_version,
    )

    seen_ops: Dict[str, OperationCandidate] = {}

    def _merge(candidates: List[OperationCandidate]) -> None:
        for c in candidates:
            if c.operation_id not in seen_ops:
                seen_ops[c.operation_id] = c
            else:
                # 合并 match_hints（后到优先）
                existing = seen_ops[c.operation_id]
                existing.match_hints.update(c.match_hints)

    if ontology is not None:
        ops = parse_ontology(ontology)
        _merge(ops)
        catalog.raw_assets["ontology"] = ontology

    if skill_manifest is not None:
        ops = parse_skill_manifest(skill_manifest)
        _merge(ops)
        catalog.raw_assets["skill_manifest"] = skill_manifest

    if tool_schema is not None:
        ops = parse_tool_schema(tool_schema)
        _merge(ops)
        catalog.raw_assets["tool_schema"] = tool_schema

    if script_list is not None:
        ops = parse_script_list(script_list)
        _merge(ops)
        catalog.raw_assets["script_list"] = script_list

    catalog.operations = sorted(seen_ops.values(), key=lambda o: o.operation_id)

    log(
        "INFO",
        "ASSET_ADAPTER",
        "资产目录构建完成",
        logical_agent_id=logical_agent_id,
        operation_count=len(catalog.operations),
    )
    return catalog


def compute_asset_hash(asset_type: str, payload: Any) -> str:
    """计算资产摘要，用于变更检测。"""
    return _hash_payload({"type": asset_type, "data": payload})


# ── 工具函数 ────────────────────────────────────────────────────


def _normalize_operation_id(raw: str) -> str:
    """将原始名归一化为点分 operation_id。

    规则：
    - 小写
    - 非字母数字替换为 _
    - 连续 _ 合并
    - 去首尾 _
    """
    import re

    slug = re.sub(r"[^a-z0-9]+", "_", (raw or "").strip().lower()).strip("_")
    # 压缩连续下划线
    slug = re.sub(r"_+", "_", slug)
    return slug or "unknown"
