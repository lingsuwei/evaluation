"""
Trace Binding Compiler（A1 / §7.5.4）：消费 AssetCatalog → 编译匹配规则 → 写入 TraceOperationRule。

核心流程：
1. 读取 AssetCatalog 中的 OperationCandidate 列表
2. 为每个候选生成 match_rules（JSONPath 格式）
3. 写入 trace_operation_rules（幂等）
4. 禁用旧版本规则
5. 更新 asset_snapshot.compile_status

设计原则：
- 不在热路径上执行（仅资产变更时触发）
- 编译结果可复现（同输入 → 同规则）
- 业务团队不维护 trace 埋点，eval 侧独立编译
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentAssetSnapshot, TraceOperationRule
from logger import log
from services.agent_asset_adapter import AssetCatalog, OperationCandidate
from services.operation_registry_service import (
    deactivate_old_rules,
    get_latest_registry_version,
    upsert_operation_rule,
    upsert_asset_snapshot,
)


# ── 规则编译 ────────────────────────────────────────────────────


def compile_match_rules(candidate: OperationCandidate) -> Dict[str, Any]:
    """将 OperationCandidate 的 match_hints 编译为 match_rules JSON（v2 JSONPath 格式）。

    生成的规则遵循 §8.8.4 格式：
    {
      "mode": "all",
      "version": "2.0",
      "rules": [
        {"type": "jsonpath", "path": "$.attributes.eval.operation.id", "op": "eq", "value": "..."},
        ...
      ]
    }
    """
    rules: List[Dict[str, Any]] = []

    # 主规则：匹配 eval.operation.id（由 A4 Builder 注入后生效）
    rules.append(
        {
            "type": "jsonpath",
            "path": "$.attributes.eval.operation.id",
            "op": "eq",
            "value": candidate.operation_id,
        }
    )

    # 从 match_hints 生成辅助匹配规则（用于 Builder 尚未注入时的降级匹配）
    hints = candidate.match_hints

    if "script_name" in hints:
        rules.append(
            {
                "type": "jsonpath",
                "path": "$.attributes.agent.script.path",
                "op": "contains",
                "value": hints["script_name"],
            }
        )

    if "agent_tool_name" in hints:
        rules.append(
            {
                "type": "jsonpath",
                "path": "$.attributes.agent.tool.name",
                "op": "eq",
                "value": hints["agent_tool_name"],
            }
        )

    if "agent_step_id" in hints:
        rules.append(
            {
                "type": "jsonpath",
                "path": "$.attributes.agent.step.id",
                "op": "eq",
                "value": hints["agent_step_id"],
            }
        )

    if "agent_step_kind" in hints:
        rules.append(
            {
                "type": "jsonpath",
                "path": "$.attributes.agent.step.kind",
                "op": "eq",
                "value": hints["agent_step_kind"],
            }
        )

    if "agent_script_path" in hints:
        rules.append(
            {
                "type": "jsonpath",
                "path": "$.attributes.agent.script.path",
                "op": "eq",
                "value": hints["agent_script_path"],
            }
        )

    return {
        "mode": "any",  # 任一规则命中即匹配（eval.operation.id 优先，其余降级）
        "version": "2.0",
        "rules": rules,
    }


def compile_binding_reason(candidate: OperationCandidate) -> str:
    """生成可展示的命中原因模板。"""
    hints = candidate.match_hints
    if "script_name" in hints:
        return f"matched script_name {hints['script_name']}"
    if "agent_tool_name" in hints:
        return f"matched tool {hints['agent_tool_name']}"
    if "agent_step_id" in hints:
        return f"matched step {hints['agent_step_id']}"
    return f"matched operation {candidate.operation_id}"


# ── 主入口 ──────────────────────────────────────────────────────


async def compile_and_persist_rules(
    session: AsyncSession,
    catalog: AssetCatalog,
    *,
    asset_snapshot_id: Optional[str] = None,
) -> int:
    """将 AssetCatalog 编译为 TraceOperationRule 并持久化。

    流程：
    1. 递增 registry_version
    2. 为每个 OperationCandidate 编译 match_rules
    3. 幂等写入 trace_operation_rules
    4. 禁用旧版本规则
    5. 更新 asset_snapshot.compile_status

    返回写入/更新的规则数量。
    """
    logical_agent_id = catalog.logical_agent_id

    # 1. 递增版本
    current_version = await get_latest_registry_version(session, logical_agent_id)
    new_version = current_version + 1

    # 2. 编译并写入
    count = 0
    for candidate in catalog.operations:
        match_rules = compile_match_rules(candidate)
        reason_template = compile_binding_reason(candidate)

        await upsert_operation_rule(
            session,
            logical_agent_id=logical_agent_id,
            operation_id=candidate.operation_id,
            match_rules=match_rules,
            display_name=candidate.display_name,
            source_asset_id=asset_snapshot_id,
            priority=0,  # 可按 source_type 调整
            confidence=1.0 if candidate.source_type in ("script", "action") else 0.9,
            binding_reason_template=reason_template,
            registry_version=new_version,
        )
        count += 1

    # 3. 禁用旧版本
    if current_version > 0:
        deactivated = await deactivate_old_rules(
            session,
            logical_agent_id=logical_agent_id,
            keep_version=new_version,
        )
        log(
            "INFO",
            "BINDING_COMPILER",
            "旧版本规则已禁用",
            logical_agent_id=logical_agent_id,
            old_version=current_version,
            new_version=new_version,
            deactivated=deactivated,
        )

    # 4. 更新 asset_snapshot.compile_status
    if asset_snapshot_id:
        snapshot = await session.get(AgentAssetSnapshot, asset_snapshot_id)
        if snapshot is not None:
            snapshot.compile_status = "compiled"
            snapshot.registry_version = new_version
            await session.flush()

    log(
        "INFO",
        "BINDING_COMPILER",
        "绑定规则编译完成",
        logical_agent_id=logical_agent_id,
        operation_count=count,
        registry_version=new_version,
    )
    return count
