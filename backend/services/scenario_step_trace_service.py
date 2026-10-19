"""场景子步骤 ↔ Langfuse observation 绑定。"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_default_model_key
from database import AgentRun, RunSpanNode, EvaluationTask
from logger import log
from services.fact_ingest_service import fetch_trace_from_langfuse
from services.langfuse_link_service import (
    build_langfuse_links_for_run,
    build_trace_urls_sync,
    resolve_langfuse_host,
)
from services.langfuse_project_registry import get_registry
from services.dataset_item_scenario_service import is_per_item_scenario_key
from services.scenario_eval_service import (
    GENERIC_L0_SCENARIO_KEY,
    _normalize_text,
    _score_step,
    get_template_by_key,
)
from services.scenario_llm_aggregate_service import (
    is_llm_configured,
    llm_aggregate_step_output,
    llm_judge_step_output,
    llm_aggregate_step_config_ready,
    parse_eval_config,
)

def _resolve_effective_llm_model(
    eval_config: Dict[str, Any],
    run: Any,
    task_llm_model: Optional[str],
) -> Optional[str]:
    """解析当前步骤实际使用的 LLM 模型 key。

    优先级：步骤配置 llm_model → run.inference_model_id → task.llm_model → 默认模型。
    关键修复：inference_model_id 可能是 onto-agent 内部 ID（如 ``global:model_xxx``），
    不在评测系统 LLM 缓存中，is_llm_configured 会返回 False。这里在 or 链解析后
    做一次校验，无效则回退到 task_llm_model / 默认模型，避免误判"未配置"。
    """
    candidate = (
        eval_config.get("llm_model")
        or getattr(run, "inference_model_id", None)
        or task_llm_model
    )
    if candidate and is_llm_configured(candidate):
        return candidate
    # inference_model_id 无效时回退
    fallback = task_llm_model or get_default_model_key()
    return fallback if fallback and is_llm_configured(fallback) else None


GENERIC_L0_FINAL_ANSWER_STEPS = frozenset({"L0-3"})

GENERIC_L0_STEP_TRACE_BINDINGS: Dict[str, Dict[str, Any]] = {
    "L0-1": {
        "span_name_patterns": ["llm:main_iteration", "llm:main", "llm:reasoning"],
        "iteration_lte": 2,
        "output_keywords": ["理解", "分析", "问题", "任务"],
        "label": "理解题意",
    },
    "L0-2": {
        "span_name_patterns": ["tool:", "llm:main_iteration", "llm:main"],
        "output_keywords": ["推理", "工具", "检索", "计算", "调用"],
        "label": "推理与工具",
    },
    "L0-3": {
        "span_name_patterns": ["tool:", "llm:main_iteration", "llm:main", "agent-inference"],
        "output_keywords": ["答案", "结论", "综上", "因此"],
        "label": "生成答案",
    },
}


def _final_answer_steps_for_scenario(
    scenario_key: str,
    tpl_data: Optional[Dict[str, Any]] = None,
) -> frozenset[str]:
    if is_per_item_scenario_key(scenario_key) and tpl_data:
        enabled_steps = [
            s
            for s in (tpl_data.get("steps") or [])
            if s.get("enabled", True)
        ]
        if enabled_steps:
            last = max(enabled_steps, key=lambda s: int(s.get("step_order") or 0))
            return frozenset({str(last.get("step_key") or "")})
        return frozenset()
    if scenario_key == GENERIC_L0_SCENARIO_KEY:
        return GENERIC_L0_FINAL_ANSWER_STEPS
    return frozenset()


# ---------------------------------------------------------------------------
# 证据集评分：本体包工作流映射
# ---------------------------------------------------------------------------


def _extract_ontology_context_key(obs: Dict[str, Any]) -> Optional[str]:
    """从 tool:ontology_context observation 提取 resource key。

    metadata.message 格式：
    - "加载本体 逻辑 临机云方案构建" → "临机云方案构建"
    - "加载本体 实体 情报" → "情报"
    - "加载本体 command-driven-tasking-formation" → None（root 加载，跳过）

    Returns:
        resource key 字符串，或 None（非 ontology_context 调用或 root 加载）
    """
    name = str(obs.get("name") or "")
    if "ontology_context" not in name.lower():
        return None

    metadata = obs.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            import json
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, ValueError):
            metadata = {}

    message = str(metadata.get("message") or "")

    if message.startswith("加载本体 逻辑 "):
        return message[len("加载本体 逻辑 "):].strip()
    if message.startswith("加载本体 实体 "):
        return message[len("加载本体 实体 "):].strip()
    # Root ontology load or unrecognized format — skip
    return None


def _build_obs_by_key_index(
    observations: List[Dict[str, Any]],
) -> Dict[str, List[str]]:
    """构建 key -> [observation_ids] 索引。

    遍历所有 observations，提取 tool:ontology_context 的 resource key，
    建立 key 到 observation_id 列表的映射。
    """
    obs_by_key: Dict[str, List[str]] = {}
    for obs in observations:
        key = _extract_ontology_context_key(obs)
        if key:
            obs_id = obs.get("id")
            if obs_id:
                obs_by_key.setdefault(key, []).append(str(obs_id))
    return obs_by_key


def build_evidence_map_from_trace(
    workflow: Any,
    observations: List[Dict[str, Any]],
    scenario_steps: List[Dict[str, Any]],
) -> Dict[str, List[str]]:
    """构建 step_key -> [observation_ids] 映射。

    映射逻辑：
    1. 遍历 observations，过滤 tool:ontology_context，提取 key
    2. 构建 key -> [observation_ids] 索引
    3. 对每个场景步骤：
       a. 步骤名匹配操作名（精确 + 子串模糊匹配）
       b. 收集该操作及其所有 [[引用]] 对应的 spans
       c. 子步骤引用也一并收集
    4. 返回 {step_key: [observation_ids]}

    Args:
        workflow: OntologyWorkflow 对象（来自 ontology_workflow_parser）
        observations: Langfuse trace observations
        scenario_steps: 场景模板步骤列表

    Returns:
        {step_key: [observation_id, ...]} 映射
    """
    from services.ontology_workflow_parser import OntologyWorkflow

    if not isinstance(workflow, OntologyWorkflow):
        log("WARNING", "EVIDENCE_SET", "Invalid workflow type")
        return {}

    # 构建 key -> [observation_ids] 索引
    obs_by_key = _build_obs_by_key_index(observations)

    if not obs_by_key:
        log("WARNING", "EVIDENCE_SET", "No ontology_context observations found")
        return {}

    log("INFO", "EVIDENCE_SET", "Built obs_by_key index",
        keys_count=len(obs_by_key),
        sample_keys=list(obs_by_key.keys())[:5])

    # 构建 step_key -> [observation_ids] 映射
    step_evidence_map: Dict[str, List[str]] = {}

    for step in scenario_steps:
        step_key = str(step.get("step_key") or "")
        step_name = str(step.get("name") or "")
        if not step_key:
            continue

        # 提取步骤名核心（去除 "步骤N：" 前缀）
        step_name_core = step_name
        for sep in ("：", ":", " - "):
            if sep in step_name_core:
                step_name_core = step_name_core.split(sep, 1)[-1].strip()

        # 查找匹配的操作
        matched_op = None
        for op_name, operation in workflow.operations.items():
            # 精确匹配
            if op_name == step_name_core:
                matched_op = operation
                break
            # 子串匹配（双向）
            if op_name in step_name_core or step_name_core in op_name:
                matched_op = operation
                break

        if not matched_op:
            continue

        # 收集该操作及其所有引用的 observation IDs
        evidence_ids: List[str] = []

        # 1. 操作自身的 spans
        if matched_op.name in obs_by_key:
            evidence_ids.extend(obs_by_key[matched_op.name])

        # 2. 操作内所有 [[引用]] 对应的 spans
        for ref in matched_op.references:
            if ref in obs_by_key:
                evidence_ids.extend(obs_by_key[ref])

        # 3. 子步骤内的引用
        for sub_step in matched_op.steps:
            for ref in sub_step.references:
                if ref in obs_by_key:
                    evidence_ids.extend(obs_by_key[ref])

        # 去重，保持顺序
        if evidence_ids:
            seen = set()
            deduped = []
            for eid in evidence_ids:
                if eid not in seen:
                    seen.add(eid)
                    deduped.append(eid)
            step_evidence_map[step_key] = deduped

    log("INFO", "EVIDENCE_SET", "Built evidence map",
        mapped_steps_count=len(step_evidence_map),
        step_keys=list(step_evidence_map.keys()))

    return step_evidence_map


def get_step_trace_binding(
    scenario_key: str,
    step_key: str,
    step_def: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """按场景与步骤定义解析 trace 绑定规则（含 match_rules.trace_binding JSONPath 主路径）。"""
    if step_def and isinstance(step_def.get("match_rules"), dict):
        tb = step_def["match_rules"].get("trace_binding")
        if isinstance(tb, dict) and tb:
            merged = dict(tb)
            merged.setdefault("label", step_def.get("name"))
            return merged
    if is_per_item_scenario_key(scenario_key) and step_def:
        gt = step_def.get("gt_expected") if isinstance(step_def.get("gt_expected"), dict) else {}
        keywords = [str(k).strip() for k in (gt.get("keywords") or []) if str(k).strip()][:8]
        if not keywords:
            criteria = step_def.get("gt_criteria") or []
            if isinstance(criteria, list):
                for c in criteria[:4]:
                    t = str(c).strip()
                    if 2 <= len(t) <= 32:
                        keywords.append(t[:32])
        return {
            "span_name_patterns": ["llm:main_iteration", "llm:main", "tool:"],
            "output_keywords": keywords[:6],
            "label": step_def.get("name") or step_key,
        }
    if scenario_key == GENERIC_L0_SCENARIO_KEY:
        return dict(GENERIC_L0_STEP_TRACE_BINDINGS.get(step_key, {}))
    return {
        "span_name_patterns": ["llm:main_iteration", "llm:main", "tool:"],
        "label": step_def.get("name") if step_def else step_key,
    }


def _json_preview(val: Any, *, max_len: Optional[int] = 4000) -> str:
    """将值转为字符串，可限制最大长度。max_len=None 时不截断。"""
    if val is None:
        return ""
    if isinstance(val, str):
        s = val.strip()
    else:
        try:
            s = json.dumps(val, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            s = str(val)
    if max_len is not None and len(s) > max_len:
        return s[: max_len - 3] + "…"
    return s


def _extract_llm_text(val: Any) -> Optional[str]:
    """从 LLM 响应/输入中提取纯文本（content + reasoning_content）。

    处理以下结构：
    - str → 原样返回（除非是 dict 的字符串表示，则尝试解析）
    - {"content": "...", "reasoning_content": "..."} → 拼接
    - {"choices": [{"message": {"content": "...", ...}}]} → OpenAI 格式
    - {"messages": [{"role": "assistant", "content": "...", ...}]} → 提取 assistant 消息
    """
    if val is None:
        return None
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        # DB 中 summary_text 可能存为 Python dict repr 字符串
        # （如 "{'content': '...', 'reasoning_content': '...'}"）
        # 可能被截断，json.loads / ast.literal_eval 会失败，改用 regex 提取
        if s.startswith("{") and ("content" in s):
            import re
            parts: List[str] = []
            for key in ("reasoning_content", "content"):
                # 分别匹配单引号（Python dict repr）和双引号（JSON）格式
                # 单引号值中可含双引号，双引号值中可含单引号
                # (?:'|$) 兼容 summary_text 被截断、缺少结束引号的情况
                m = re.search(
                    rf"'{re.escape(key)}'\s*:\s*'((?:[^'\\]|\\.)*)(?:'|$)",
                    s, re.DOTALL,
                )
                if not m:
                    m = re.search(
                        rf'"{re.escape(key)}"\s*:\s*"((?:[^"\\]|\\.)*)(?:"|$)',
                        s, re.DOTALL,
                    )
                if m:
                    raw = m.group(1)
                    # 反转义常见转义序列
                    raw = raw.replace("\\n", "\n").replace("\\t", "\t").replace("\\'", "'").replace('\\"', '"')
                    parts.append(raw)
            if parts:
                return "\n".join(parts)
        return s

    if isinstance(val, dict):
        # OpenAI chat completion 格式
        choices = val.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict):
                return _extract_llm_text(msg)

        # 直接含 content / reasoning_content
        parts: List[str] = []
        for k in ("reasoning_content", "content"):
            v = val.get(k)
            if v and isinstance(v, str):
                parts.append(v.strip())
        if parts:
            return "\n".join(parts)

        # messages 数组 → 提取 assistant 消息
        messages = val.get("messages")
        if isinstance(messages, list):
            asst_parts: List[str] = []
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "assistant":
                    txt = _extract_llm_text(m)
                    if txt:
                        asst_parts.append(txt)
            if asst_parts:
                return "\n".join(asst_parts)

    return None


def _observation_text_blob(obs: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("input", "output", "metadata"):
        val = obs.get(key)
        if val is not None:
            # 优先提取 LLM 纯文本，提取不到时降级到 JSON 序列化
            text = _extract_llm_text(val)
            parts.append(text if text else _json_preview(val, max_len=8000))
    return _normalize_text(" ".join(parts))


def _observation_output_blob(obs: Dict[str, Any]) -> str:
    """提取 observation 的输出文本（仅 output + metadata.output，不含 input）。

    用于关键词匹配（bind_step_observation），避免 tool span 的 input
    （查询参数/本体数据/用户 query）造成误命中。

    output fallback 逻辑与 extract_observation_io / _effective_output_len 一致：
    当 obs.output 很短而 metadata.output 更丰富时，使用 metadata.output。
    """
    out = obs.get("output")
    meta = obs.get("metadata")
    if isinstance(meta, dict):
        meta_out = meta.get("output")
        if meta_out is not None and _content_less_than(out, meta_out):
            out = meta_out
    if out is None:
        return ""
    text = _extract_llm_text(out)
    if text:
        return _normalize_text(text)
    return _normalize_text(_json_preview(out, max_len=8000))


def _observation_input_semantic(obs: Dict[str, Any]) -> Tuple[str, str]:
    """提取 observation 的 tool input 结构化语义字段（子步骤绑定的强信号）。

    与 _observation_output_blob 互补：output blob 只取输出文本，本函数提取 tool
    调用 input 中的结构化字段（key/entity/query/operation_key 等），这些字段直接
    表明该 span 在查询什么规则或什么业务数据，是子步骤绑定的最强语义信号。

    返回 (semantic_text, tool_kind)：
    - tool_kind ∈ {"ontology_rule","data_search","script","other_tool","unknown"}
    - semantic_text 为归一化后的结构化字段拼接文本（空则 ""）

    只提取已知结构化字段，不拼接整段 input，避免用户 query 噪音造成误命中。
    """
    name = str(obs.get("name") or "").lower()
    otype = str(obs.get("type") or obs.get("observation_type") or "").upper()

    if "ontology_context" in name:
        tool_kind = "ontology_rule"
    elif "business_data_search" in name or "data_search" in name:
        tool_kind = "data_search"
    elif "execute_script" in name:
        tool_kind = "script"
    elif otype == "TOOL" or name.startswith("tool:"):
        tool_kind = "other_tool"
    else:
        return "", "unknown"

    inp = obs.get("input")
    if inp is None:
        return "", tool_kind
    if isinstance(inp, str) and inp.strip():
        try:
            inp = json.loads(inp)
        except (json.JSONDecodeError, TypeError):
            return "", tool_kind
    if not isinstance(inp, dict):
        return "", tool_kind

    parts: List[str] = []
    if tool_kind == "ontology_rule":
        key = inp.get("key") or inp.get("operation_key") or inp.get("object_key")
        if isinstance(key, str) and key.strip():
            parts.append(key.strip())
    elif tool_kind == "data_search":
        entity = inp.get("entity")
        query = inp.get("query")
        if isinstance(entity, str) and entity.strip():
            parts.append(entity.strip())
        if isinstance(query, str) and query.strip():
            parts.append(query.strip())
    elif tool_kind == "script":
        ok = inp.get("operation_key")
        if isinstance(ok, str) and ok.strip():
            parts.append(ok.strip())
    else:
        for field in ("operation_key", "object_key", "resource", "action", "entity"):
            val = inp.get(field)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())

    if not parts:
        return "", tool_kind
    return _normalize_text(" ".join(parts)), tool_kind


def _input_semantic_score(
    obs: Dict[str, Any],
    binding: Dict[str, Any],
) -> Tuple[float, Dict[str, Any]]:
    """计算 tool input 语义匹配得分（强信号，与 output 关键词互补）。

    打分规则：
    - ontology_rule: input.key 与步骤 label/名 完全匹配（去空格子串）→ +20（一票决定级）
    - data_search: input.entity/query 命中关键词 +4/个，上限 +12
    - script/other_tool: 语义字段命中关键词 +2/个，上限 +8
    - unknown: 0（走 output 关键词通道）
    """
    semantic_text, tool_kind = _observation_input_semantic(obs)
    if not semantic_text or tool_kind == "unknown":
        return 0.0, {"reason": "no_input_semantic", "tool_kind": tool_kind}

    label = str(binding.get("label") or "").strip()
    keywords = binding.get("output_keywords") or []
    sem_stripped = semantic_text.replace(" ", "")
    label_stripped = label.replace(" ", "") if label else ""

    if tool_kind == "ontology_rule":
        # input.key 与步骤 label/名 匹配 → 强信号
        # label 形如 "步骤4： 潜在威胁提取"，取 "：" 后的核心名
        # input.key 可能等于 label_core（如 "潜在威胁提取"），
        # 也可能是 label_core 的子串（如 input.key="装备选型"，label_core="装备选型校验"）
        label_core = label_stripped
        for sep in ("：", ":"):
            if sep in label_core:
                label_core = label_core.split(sep, 1)[1]
        matched = False
        if label_core and sem_stripped:
            # 双向子串匹配：较短一方长度 ≥ 3，避免单字误匹配
            if len(sem_stripped) >= 3 and sem_stripped in label_core:
                matched = True
            elif len(label_core) >= 3 and label_core in sem_stripped:
                matched = True
        if matched:
            return 20.0, {
                "match_type": "ontology_key_exact",
                "matched_field": "key",
                "matched_value": semantic_text,
                "tool_kind": tool_kind,
            }
        return 0.0, {"reason": "ontology_key_no_match", "tool_kind": tool_kind}

    hits = [k for k in keywords if k.replace(" ", "") in sem_stripped]
    if not hits:
        return 0.0, {"reason": "no_keyword_in_input", "tool_kind": tool_kind}

    if tool_kind == "data_search":
        per_hit, cap = 4.0, 12.0
    else:
        per_hit, cap = 2.0, 8.0
    score = min(cap, len(hits) * per_hit)
    return score, {
        "match_type": f"{tool_kind}_keyword_hit",
        "matched_keywords": hits,
        "tool_kind": tool_kind,
    }


def _observation_step_index(obs: Dict[str, Any]) -> Optional[int]:
    """从 observation 的 input 中提取 step_index 字段（onto-agent tool span 特有）。"""
    inp = obs.get("input")
    # input 可能是 JSON 字符串（Langfuse 某些场景），先尝试解析
    if isinstance(inp, str) and inp.strip():
        try:
            inp = json.loads(inp)
        except (json.JSONDecodeError, TypeError):
            pass
    if isinstance(inp, dict):
        si = inp.get("step_index")
        if si is not None:
            try:
                return int(si)
            except (TypeError, ValueError):
                pass
    meta = obs.get("metadata")
    if isinstance(meta, dict):
        si = meta.get("step_index")
        if si is not None:
            try:
                return int(si)
            except (TypeError, ValueError):
                pass
    return None


def _observation_iteration(obs: Dict[str, Any]) -> Optional[int]:
    meta = obs.get("metadata")
    if isinstance(meta, dict):
        it = meta.get("iteration")
        if it is not None:
            try:
                return int(it)
            except (TypeError, ValueError):
                pass
    tags = obs.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            m = re.search(r"iteration_(\d+)", str(tag))
            if m:
                return int(m.group(1))
    return None


def _step_index_proximity(obs: Dict[str, Any], expected: Optional[int]) -> int:
    """计算 observation 的 step_index 与期望值的邻近度得分。

    返回 100 表示精确匹配，否则按距离衰减：距离每远 1 减 10 分。
    无 step_index 的 observation 返回 -1（最低优先级）。
    """
    if expected is None:
        return 0
    si = _observation_step_index(obs)
    if si is None:
        return -1
    distance = abs(si - expected)
    return max(0, 100 - distance * 10)


def _span_row_to_observation(row: RunSpanNode) -> Dict[str, Any]:
    attrs = row.attributes if isinstance(row.attributes, dict) else {}
    meta = dict(attrs)
    if row.step_index is not None:
        meta.setdefault("step_index", row.step_index)
    return {
        "id": row.observation_id or row.id,
        "name": row.name,
        "type": row.node_type,
        "input": attrs.get("input"),
        "output": attrs.get("output") or row.summary_text,
        "metadata": meta,
        "startTime": row.started_at.isoformat() if row.started_at else None,
    }


def _apply_attribute_mapping(
    observations: List[Dict[str, Any]],
    provider: Optional[str],
) -> List[Dict[str, Any]]:
    """对 Langfuse API 原始 observation 列表应用 OTel 属性映射。

    与 span_tree_builder.build_span_node_rows() 中的映射逻辑对齐，
    确保 bind_step_observation() 在 Langfuse API 路径也能获得 agent.step.id
    和 aggregation_role 等映射属性。

    映射后的属性写回 observation.metadata（不覆盖已有键），
    供 _agent_step_id_from_obs() / _is_container_observation() 读取。
    """
    if not provider:
        return observations

    try:
        from services.otel_mapping_engine import create_mapping_engine
        from services.otel_attributes import normalize_observation_attributes
    except ImportError:
        return observations

    engine = create_mapping_engine(provider)
    if not engine:
        return observations

    for obs in observations:
        if not isinstance(obs, dict):
            continue
        attrs = normalize_observation_attributes(obs)
        attrs = engine.apply(obs, attrs)
        # 把映射后新增的属性写回 observation.metadata
        meta = obs.get("metadata")
        if not isinstance(meta, dict):
            meta = {}
            obs["metadata"] = meta
        for k, v in attrs.items():
            if k not in meta:
                meta[k] = v
    return observations


def _is_container_observation(obs: Dict[str, Any]) -> bool:
    """判断是否为容器 span（tool_batch / iteration / agent_loop）。

    onto-agent 的并行工具执行把多个 tool 聚合在 tool_batch span 下，
    该容器不包含具体步骤的 input/output，不应参与子步骤匹配。

    判定方式（双重保险）：
    1. metadata.aggregation_role == "container"（经 _apply_attribute_mapping 映射后写入）
    2. 兜底：未应用映射时按 name 前缀 + type 识别
    """
    meta = obs.get("metadata")
    if isinstance(meta, dict) and meta.get("aggregation_role") == "container":
        return True
    name = str(obs.get("name") or "")
    obs_type = str(obs.get("type") or obs.get("observation_type") or "").upper()
    if obs_type in ("SPAN",):
        for prefix in ("tools:", "iteration:", "agent_loop:"):
            if name.startswith(prefix):
                return True
    return False


async def load_observations_for_run(
    session: AsyncSession,
    run: AgentRun,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    加载 observation 列表。优先 Langfuse API（含完整 input/output），
    失败时降级 run_span_nodes。

    Langfuse API 路径会对原始 observation 应用 OTel 属性映射
    （与 span_tree_builder 物化路径对齐），使 bind_step_observation()
    在 API 路径也能获得 agent.step.id / aggregation_role 等映射属性。
    """
    trace_id = (run.trace_id or run.root_trace_id or "").strip()
    langfuse_project = (run.langfuse_project or "").strip() or None

    if trace_id:
        trace = await fetch_trace_from_langfuse(trace_id, langfuse_project)
        if trace:
            obs = trace.get("observations") or []
            if isinstance(obs, list) and obs:
                sorted_obs = sorted(
                    [o for o in obs if isinstance(o, dict)],
                    key=lambda o: str(o.get("startTime") or o.get("start_time") or ""),
                )
                sorted_obs = _apply_attribute_mapping(sorted_obs, run.logical_agent_id)
                return sorted_obs, "langfuse_api"

    spans = (
        await session.execute(
            select(RunSpanNode)
            .where(RunSpanNode.agent_run_id == run.id)
            .order_by(RunSpanNode.step_index, RunSpanNode.sibling_order)
        )
    ).scalars().all()
    if spans:
        return [_span_row_to_observation(s) for s in spans], "run_span_nodes"

    return [], "none"


def _name_matches(name: str, patterns: List[str], obs_type: str = "") -> bool:
    """检查 observation 名称是否匹配 patterns。

    支持两种匹配维度：
    1. 名称子串匹配（原有逻辑）：pattern 在 name 中出现即可。
    2. 类型回退匹配（兼容 agent_swarm 等无前缀命名）：
       - pattern 以 ``tool:`` 开头时，若名称不匹配，回退匹配 obs_type == "TOOL"。
       - pattern 以 ``llm:`` 开头时，若名称不匹配，回退匹配 obs_type == "GENERATION"。
       onto-agent 的 span 名形如 ``tool:ask_ontology`` / ``llm:main_iteration``（带前缀），
       agent_swarm 的 span 名形如 ``ask_remote_agent`` / ``llm_reasoning:1``（无前缀），
       类型回退使后者也能被 patterns 命中。
    """
    n = (name or "").strip().lower()
    ot = (obs_type or "").strip().upper()
    for p in patterns:
        pl = p.lower()
        if n == pl or n.startswith(pl) or pl in n:
            return True
        # 类型回退：agent_swarm 的 tool/llm span 名无前缀，按 type 兜底
        if pl.startswith("tool:") and ot == "TOOL":
            return True
        if pl.startswith("llm:") and ot == "GENERATION":
            return True
    return False


def _agent_step_id_from_obs(obs: Dict[str, Any]) -> Optional[str]:
    meta = obs.get("metadata")
    if isinstance(meta, dict):
        sid = meta.get("agent.step.id") or meta.get("agent_step_id")
        if sid:
            return str(sid).strip()
    return None


def _extract_step_agent_name(binding: Dict[str, Any]) -> str:
    """从 binding 中提取步骤关联的智能体名称。

    优先级：
    1. ``binding["agent_name"]`` — trace_binding 中显式配置
    2. ``binding["label"]`` 中 "：" / ":" 前的前缀（如 "网云协同智能体：请..." → "网云协同智能体"）
    """
    name = (binding.get("agent_name") or "").strip()
    if name:
        return name
    label = (binding.get("label") or "").strip()
    if not label:
        return ""
    for sep in ("：", ":"):
        idx = label.find(sep)
        if idx > 0:
            prefix = label[:idx].strip()
            if 2 <= len(prefix) <= 32:
                return prefix
    return ""


def _observation_agent_names(obs: Dict[str, Any]) -> set:
    """从 observation 中提取所有关联的智能体名称集合。

    检查维度：
    - metadata.source_agent_name / target_agent（A2A 轮次 + tool span）
    - name 中的 ``agent_turn:a2a:{name}`` 段
    - input 字符串中的 ``target_agent': 'XXX'`` （agent_swarm 把 args 字符串化）
    - output 字符串中的 ``from XXX`` / ``matched target_agent=XXX``
    """
    names: set = set()
    meta = obs.get("metadata")
    if isinstance(meta, dict):
        for key in ("source_agent_name", "target_agent", "agent.member.name"):
            v = meta.get(key)
            if v and isinstance(v, str) and v.strip():
                names.add(v.strip())
    name = str(obs.get("name") or "")
    if "agent_turn:a2a:" in name:
        seg = name.split("agent_turn:a2a:", 1)[1]
        # 取后半段（去掉 queued: 前缀）
        if seg.startswith("queued:"):
            seg = seg[len("queued:"):]
        seg = seg.strip()
        if seg:
            names.add(seg)
    blob = _observation_text_blob(obs)
    if blob:
        import re
        # agent_swarm chat.py 把 tool_args 字符串化：{'target_agent': '战管协同智能体', ...}
        for m in re.finditer(r"target_agent['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]", blob):
            names.add(m.group(1).strip())
        # A2A delegation result output：[A2A delegation result from 网云协同智能体]
        for m in re.finditer(r"from\s+([^\]\s][^\]]*?)(?:\]|\n|$)", blob):
            candidate = m.group(1).strip().rstrip("]")
            if 2 <= len(candidate) <= 64 and "智能体" in candidate:
                names.add(candidate)
        # Route reason: matched target_agent=网云协同智能体
        for m in re.finditer(r"matched\s+target_agent=([^\s,\]]+)", blob):
            names.add(m.group(1).strip())
    return names


def _content_less_than(a: Any, b: Any) -> bool:
    """判断 a 的内容是否明显少于 b（用于 output vs metadata.output 比较）。"""
    if a is None:
        return True
    if b is None:
        return False
    # 如果 b 是 dict/list 而 a 只是短字符串，a 明显更少
    if isinstance(b, (dict, list)):
        if isinstance(a, str) and len(a.strip()) < 200:
            return True
        if not isinstance(a, (dict, list)):
            return True
    len_a = len(str(a))
    len_b = len(str(b))
    # b 长度显著大于 a（至少 2 倍且多 500 字符以上）
    return len_b > len_a * 2 and len_b - len_a > 500


def _effective_output_len(obs: Dict[str, Any]) -> int:
    """获取 observation 的真实输出内容长度（含 metadata.output fallback）。"""
    out = obs.get("output")
    meta = obs.get("metadata")
    # 与 extract_observation_io 保持一致的 fallback 逻辑
    if isinstance(meta, dict):
        meta_out = meta.get("output")
        if meta_out is not None and _content_less_than(out, meta_out):
            out = meta_out
    # 计算长度
    if out is None:
        return 0
    # 优先提取纯文本计算长度，避免 JSON 语法噪声影响
    text = _extract_llm_text(out)
    if text:
        return len(text)
    if isinstance(out, (dict, list)):
        try:
            return len(json.dumps(out, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            return 0
    return len(str(out))


def bind_step_observation(
    step_key: str,
    observations: List[Dict[str, Any]],
    *,
    binding: Optional[Dict[str, Any]] = None,
    final_answer_text: str = "",
    final_answer_steps: Optional[frozenset[str]] = None,
    used_observation_ids: Optional[set] = None,
    peer_step_keywords: Optional[Dict[str, List[str]]] = None,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """为单个子步骤选取最佳 observation。

    used_observation_ids: 已被前序步骤匹配的 observation ID 集合，
    避免多个步骤绑定到同一个 observation。

    peer_step_keywords: 同场景其他步骤的 output_keywords（不含本步骤），
    用于计算跨步骤命中数 mo（observation output 命中多少其他步骤的关键词）。
    在主得分 score 平局时作为 tiebreaker（-mo，mo 越小越优先），抑制
    "终局合并答案"等跨步骤混合 span 抢占单步 span。仅 tiebreaker 层生效，
    不进入主得分，避免跨步骤重复关键词（如 WPN/HEAT）导致的误降权。
    """
    if binding is None:
        binding = get_step_trace_binding("", step_key)
    else:
        binding = dict(binding)
    patterns = binding.get("span_name_patterns") or []
    keywords = binding.get("output_keywords") or []
    iteration_lte = binding.get("iteration_lte")
    iteration_lte_llm_only = bool(binding.get("iteration_lte_llm_only"))
    agent_step_id = (binding.get("agent_step_id") or "").strip()
    expected_step_index = binding.get("expected_step_index")
    step_agent_name = _extract_step_agent_name(binding)
    exclude_set = used_observation_ids or set()

    # 过滤容器 span（tool_batch / iteration / agent_loop）。
    # onto-agent 的并行工具执行把多个 tool 聚合在 tool_batch span 下，
    # 容器不包含具体步骤的 input/output，参与匹配会抢占叶子 tool span 的绑定。
    matchable_observations = [
        obs for obs in observations if not _is_container_observation(obs)
    ]

    candidates: List[Dict[str, Any]] = []
    # 第一优先级：通过 step_index 精确匹配（仅当步骤配置显式声明 expected_step_index 时启用）
    # 注意：step_index 与场景步骤无契约对齐关系，命中后仍须验证关键词相关性，
    # 避免绑到"序号恰好相同但内容无关"的 span（如 llm:reasoning_summary / 状态摘要 span）。
    # 使用局部 si_candidates，避免 score=100 的候选污染后续 heuristic 分支的 max(candidates)。
    if expected_step_index is not None:
        si_candidates: List[Dict[str, Any]] = []
        for obs in matchable_observations:
            obs_id = obs.get("id")
            if obs_id and obs_id in exclude_set:
                continue
            name = str(obs.get("name") or "")
            otype = str(obs.get("type") or obs.get("observation_type") or "")
            if patterns and not _name_matches(name, patterns, otype):
                continue
            si = _observation_step_index(obs)
            if si is not None and si == int(expected_step_index):
                blob = _observation_output_blob(obs)
                real_kw_hits = [k for k in keywords if k.replace(" ", "") in blob]
                si_candidates.append(
                    {
                        "obs": obs,
                        "obs_id": obs_id,
                        "score": 100,
                        "kw_hits": real_kw_hits,
                        "iteration": _observation_iteration(obs),
                        "name": name,
                    }
                )
        if si_candidates:
            best = max(
                si_candidates,
                # 优先：关键词命中数多 → 输出内容多的 observation
                key=lambda c: (
                    c["score"],
                    len(c["kw_hits"]),
                    _effective_output_len(c["obs"]),
                    -(c["iteration"] or 999),
                ),
            )
            # 仅当 step_index 命中的 span 同时命中关键词时才短路返回；
            # 否则 step_index 可能只是 span 树 DFS 序号或 agent 自身执行序号，
            # 与场景步骤无契约关系，继续走 agent_name / heuristic 分支。
            if best["kw_hits"]:
                meta = {
                    "bind_source": "step_index",
                    "step_key": step_key,
                    "candidates_count": len(si_candidates),
                    "matched_keywords": best["kw_hits"],
                    "observation_name": best["name"],
                    "observation_id": best.get("obs_id"),
                    "iteration": best["iteration"],
                }
                # 多个候选时记录备选信息，辅助排查绑定错误
                if len(si_candidates) > 1:
                    meta["step_index_candidates"] = [
                        {
                            "obs_id": c.get("obs_id"),
                            "name": c["name"],
                            "iteration": c["iteration"],
                            "kw_hit_count": len(c["kw_hits"]),
                            "name_len": len(c["name"]),
                        }
                        for c in sorted(si_candidates, key=lambda c: (c["iteration"] or 999, -len(c["name"])))
                    ]
                return best["obs"], meta
            # 关键词未命中：step_index 候选不可信，落到 agent_name / heuristic 分支

    # 第 1.5 优先级：通过智能体名称匹配 observation（多智能体协作 trace）
    # 当步骤名含智能体名称（如"网云协同智能体：..."）时，先按名称筛选该
    # agent 对应的 observation 子树（ask_remote_agent tool span / A2A turn），
    # 再在子树内做关键词打分，避免不同 agent 的节点互相抢错。
    if step_agent_name:
        agent_candidates: List[Dict[str, Any]] = []
        for obs in matchable_observations:
            obs_id = obs.get("id")
            if obs_id and obs_id in exclude_set:
                continue
            obs_agent_names = _observation_agent_names(obs)
            if step_agent_name not in obs_agent_names:
                continue
            name = str(obs.get("name") or "")
            otype = str(obs.get("type") or obs.get("observation_type") or "")
            # 不做 patterns 过滤：agent_swarm 的 span 名（ask_remote_agent）
            # 可能不匹配 patterns（tool:），但 agent 名称已精准定位
            blob = _observation_output_blob(obs)
            kw_hits = [k for k in keywords if k.replace(" ", "") in blob]
            # agent 名称命中给基础分 50，关键词每个 +1，确保高于无 agent 信息的候选
            score = 50 + len(kw_hits)
            agent_candidates.append(
                {
                    "obs": obs,
                    "obs_id": obs_id,
                    "score": score,
                    "kw_hits": kw_hits,
                    "iteration": _observation_iteration(obs),
                    "name": name,
                }
            )
        if agent_candidates:
            best = max(
                agent_candidates,
                key=lambda c: (
                    c["score"],
                    _step_index_proximity(c["obs"], expected_step_index),
                    _effective_output_len(c["obs"]),
                    -(c["iteration"] or 999),
                ),
            )
            meta = {
                "bind_source": "agent_name",
                "step_key": step_key,
                "step_agent_name": step_agent_name,
                "candidates_count": len(agent_candidates),
                "matched_keywords": best["kw_hits"],
                "observation_name": best["name"],
                "observation_id": best.get("obs_id"),
                "iteration": best["iteration"],
            }
            return best["obs"], meta

    # 第二优先级：关键词启发式匹配
    for obs in matchable_observations:
        obs_id = obs.get("id")
        if obs_id and obs_id in exclude_set:
            continue
        if agent_step_id:
            sid = _agent_step_id_from_obs(obs)
            if sid != agent_step_id:
                continue
        name = str(obs.get("name") or "")
        otype = str(obs.get("type") or obs.get("observation_type") or "")
        if patterns and not _name_matches(name, patterns, otype):
            continue
        it = _observation_iteration(obs)
        if iteration_lte is not None and it is not None and it > int(iteration_lte):
            is_tool_span = name.lower().startswith("tool:") or otype == "TOOL"
            if not (iteration_lte_llm_only and is_tool_span):
                continue
        blob = _observation_output_blob(obs)
        kw_hits = [k for k in keywords if k.replace(" ", "") in blob]
        # 跨步骤命中数 mo：该 observation output 命中了多少其他步骤的关键词。
        # 用于 score 平局时作 tiebreaker（-mo），抑制"终局合并答案"等
        # 跨步骤混合 span 在 own score 相同时抢占单步骤专一 span。
        # 真实 trace b59b1fd5 Q2-S2：iter20(终局合并答案) 与 iter17(纯S2)
        # own score 均 5，但 iter20 命中 S1+S3（mo=3），iter17 仅顺带 S1（mo=2），
        # -mo tiebreaker 选 iter17，纠正错绑。
        mo = 0
        if peer_step_keywords:
            for peer_kws in peer_step_keywords.values():
                if any(k.replace(" ", "") in blob for k in peer_kws):
                    mo += 1
        # 分层信号模型（层间不穿透）：
        # - PRIMARY: output 关键词命中（按 tool 类型降权），是评测阶段唯一可用的信号
        # - SECONDARY: input 语义匹配（仅作 tiebreaker，不进入主得分）
        # input_score 不进入主得分，避免"1个kw + 高input_score 压过 3个kw + 无input_score"
        # （真实 trace 75a60194 Q1-S1 回归：input.entity 命中字段名"能力标签"得 input_score=4，
        # 但 output 只有 1 个 kw，却压过 llm:main_iteration 的 3 个 kw 命中）
        input_score, input_details = _input_semantic_score(obs, binding)
        tool_kind = input_details.get("tool_kind", "unknown")
        kw_weight = 0.3 if tool_kind == "ontology_rule" else 1.0
        weighted_kw = round(len(kw_hits) * kw_weight, 2)
        score = (
            weighted_kw
            + (3 if agent_step_id and _agent_step_id_from_obs(obs) == agent_step_id else 0)
        )
        candidates.append(
            {
                "obs": obs,
                "obs_id": obs_id,
                "score": score,
                "input_score": input_score,
                "input_details": input_details,
                "tool_kind": tool_kind,
                "kw_weight": kw_weight,
                "kw_hits": kw_hits,
                "cross_step_hits": mo,
                "iteration": it,
                "name": name,
            }
        )

    meta: Dict[str, Any] = {
        "bind_source": "heuristic",
        "step_key": step_key,
        "candidates_count": len(candidates),
    }
    if agent_step_id:
        meta["agent_step_id"] = agent_step_id

    if candidates:
        # 排序优先级（分层，层间不穿透）：
        # 1. PRIMARY：综合得分（降权 output 关键词 + agent_step_id），不含 input_score
        # 2. SECONDARY：input 语义匹配强度（结构化语义信号，tiebreaker）
        # 3. 跨步骤纯度（-cross_step_hits）：own score 平局时，优先选对其他步骤
        #    关键词命中更少的 span，抑制"终局合并答案"等跨步骤混合 span 抢占
        #    单步骤专一 span。仅 tiebreaker 层生效，不进入主得分。
        # 4. TERTIARY：step_index 邻近度 + output_len(cap 5000) + iteration
        # output_len cap=5000 防止规则文档/LLM 推理（10KB+）压过数据查询结果，
        # 同时在所有 span 都 0 kw 命中时优先选实质内容更多的 span。
        best = max(
            candidates,
            key=lambda c: (
                c["score"],
                c["input_score"],
                -c.get("cross_step_hits", 0),
                _step_index_proximity(c["obs"], expected_step_index),
                min(_effective_output_len(c["obs"]), 5000),
                -(c["iteration"] or 999),
            ),
        )
        meta["matched_keywords"] = best["kw_hits"]
        meta["observation_name"] = best["name"]
        meta["observation_id"] = best.get("obs_id")
        meta["iteration"] = best["iteration"]
        meta["input_score"] = best["input_score"]
        meta["tool_kind"] = best["tool_kind"]
        meta["cross_step_hits"] = best.get("cross_step_hits", 0)
        if agent_step_id and _agent_step_id_from_obs(best["obs"]) == agent_step_id:
            meta["bind_source"] = "agent_step_id"
            return best["obs"], meta
        if best["score"] > 0:
            meta["bind_source"] = "heuristic"
            return best["obs"], meta
        # score=0：关键词未命中任何候选 observation。
        # onto-agent 并行处理：多个步骤的结果可能在一个 observation 中，或跨了 observation，
        # 某些步骤的 evidence 不在单个 span 中，仅在终局答案（agent_answer）中汇总。
        # 此时用终局答案评测比用无关 span output 更准确。
        # 保留 best span 的 observation_id 供调试和 used_obs_ids 去重（不清空），
        # 但 return None → _resolve_step_score_text 改走终局答案评测。
        if final_answer_text:
            meta["bind_source"] = "final_answer_no_span"
            meta["fallback_reason"] = "no_keyword_match_use_final_answer"
            return None, meta
        meta["bind_source"] = "heuristic_no_keyword"
        return best["obs"], meta

    fa_steps = final_answer_steps or frozenset()
    if (
        step_key in fa_steps
        and final_answer_text
        and keywords
    ):
        norm = _normalize_text(final_answer_text)
        hits = [k for k in keywords if k.replace(" ", "") in norm]
        if hits:
            meta["bind_source"] = "final_answer_fallback"
            meta["matched_keywords"] = hits
            return None, meta

    # 兜底：observations 非空但 candidates 为空（patterns 不匹配或 iteration 过滤）时，
    # 强制绑定第一个可用 observation，避免 LLM 评测因「无 Agent 输出」直接判 0 分。
    # 这在 onto-agent tool span 名字格式与预期 patterns 不一致时尤其有效。
    if matchable_observations and not candidates:
        for fallback_obs in matchable_observations:
            f_obs_id = fallback_obs.get("id")
            if f_obs_id and f_obs_id in exclude_set:
                continue
            meta["bind_source"] = "fallback_observation"
            meta["observation_name"] = str(fallback_obs.get("name") or "")
            meta["observation_id"] = f_obs_id
            return fallback_obs, meta

    meta["bind_source"] = "unbound"
    return None, meta


def extract_observation_io(obs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not obs:
        return {
            "input_preview": "",
            "output_preview": "",
            "observation_id": None,
            "observation_name": None,
            "observation_type": None,
        }
    inp = obs.get("input")
    out = obs.get("output")
    meta = obs.get("metadata")
    # 当 obs.output 很短且 metadata.output 有更多内容时，优先用 metadata.output
    # （某些 onto-agent tool span 在 obs.output 只放状态摘要，完整数据在 metadata 中）
    if isinstance(meta, dict):
        meta_out = meta.get("output")
        if meta_out is not None and _content_less_than(out, meta_out):
            out = meta_out
        elif out is None and meta_out is not None:
            out = meta_out
    # output 仍为空时，尝试从 metadata 整体提取（部分 tool span 把结果放在 metadata 里）
    if out is None and isinstance(meta, dict) and meta:
        out = meta
    # 从 LLM 响应/输入中提取纯文本（content + reasoning_content），
    # 避免 score_text 为 JSON 格式影响关键词匹配和 LLM judge 评判
    out_text = _extract_llm_text(out)
    if out_text:
        out = out_text
    inp_text = _extract_llm_text(inp)
    if inp_text:
        inp = inp_text
    return {
        "input_preview": _json_preview(inp, max_len=None),
        "output_preview": _json_preview(out, max_len=None),
        "observation_id": obs.get("id"),
        "observation_name": obs.get("name"),
        "observation_type": obs.get("type"),
    }


def build_langfuse_trace_urls(
    *,
    trace_id: str,
    observation_id: Optional[str],
    langfuse_project: Optional[str],
    langfuse_project_id: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """兼容旧调用；优先传入 langfuse_project_id。"""
    pid = langfuse_project_id
    host: Optional[str] = None
    if langfuse_project:
        try:
            cfg = get_registry().get_config(langfuse_project)
            if pid is None:
                pid = cfg.langfuse_project_id
            if cfg.host:
                host = cfg.host
        except Exception:
            pass
    return build_trace_urls_sync(
        trace_id=trace_id,
        langfuse_project_id=pid,
        observation_id=observation_id,
        host=host,
    )


def _resolve_step_score_text(
    step_key: str,
    io: Dict[str, Any],
    bind_meta: Dict[str, Any],
    agent_answer: str,
    *,
    final_answer_steps: frozenset[str] = frozenset(),
) -> Tuple[str, str]:
    """
    返回 (score_text, score_source)。
    禁止用 ground_truth；span 已绑定时只用 span output，不回退整段答案（避免 S8 未命中仍满分）。
    """
    bind_source = bind_meta.get("bind_source") or "unbound"
    output = (io.get("output_preview") or "").strip()
    inp = (io.get("input_preview") or "").strip()
    # tool 类 span 常把 object_key / 参数放在 input，仅看 output 会误判 0 分
    if inp and inp not in output:
        span_text = f"{inp}\n{output}".strip() if output else inp
    else:
        span_text = output

    if bind_source in (
        "heuristic",
        "heuristic_no_keyword",
        "agent_step_id",
        "fallback_observation",
        "evidence_set",  # 证据集评分：合并多个 span 的文本
    ):
        return span_text, "span_output"

    if bind_source == "final_answer_fallback":
        if step_key in final_answer_steps:
            return agent_answer, "final_answer"
        return "", "final_answer_rejected"

    if bind_source == "final_answer_no_span":
        # onto-agent 并行处理：步骤 evidence 跨多个 span，单个 span 0 命中关键词，
        # 改用终局答案（agent_answer）评测——终局答案汇总了所有步骤结果。
        return agent_answer, "final_answer"

    if bind_source == "unbound":
        if step_key in final_answer_steps:
            return agent_answer, "final_answer_unbound"
        return "", "unbound"

    return output, bind_source


def _observation_index(
    observations: List[Dict[str, Any]], observation_id: Optional[str]
) -> Optional[int]:
    if not observation_id:
        return None
    for idx, obs in enumerate(observations):
        if obs.get("id") == observation_id:
            return idx
    return None


def _apply_scenario_enforce_order(
    observations: List[Dict[str, Any]],
    step_results: List[Dict[str, Any]],
) -> None:
    """启用顺序依赖的子步骤：Trace 绑定位置须严格递增（相对 step_order）。"""
    ordered = sorted(
        step_results,
        key=lambda r: (
            r.get("step_order") if r.get("step_order") is not None else 10_000,
            r.get("step_key") or "",
        ),
    )
    last_idx = -1
    for row in ordered:
        if not row.get("enforce_order"):
            continue
        obs_id = (row.get("trace_binding") or {}).get("observation_id")
        idx = _observation_index(observations, obs_id)
        if idx is None:
            continue
        if idx <= last_idx:
            row["evidence"]["order_ok"] = False
            row["passed"] = False
            note = "启用顺序依赖：绑定 span 出现顺序早于前序必做步骤"
            row["evidence"]["note"] = note
        else:
            row["evidence"]["order_ok"] = True
            last_idx = idx


async def compute_run_scenario_steps(
    session: AsyncSession,
    run: AgentRun,
    tpl_data: Dict[str, Any],
    agent_answer: str,
    *,
    persist_span_refs: bool = True,
    skip_llm_judge: bool = False,
    _emit_step_event=None,
    _task_id: Optional[str] = None,
    _current_step_ref=None,
    _total_steps: int = 0,
) -> Dict[str, Any]:
    """统一子步骤判分：trace 绑定 + 仅 agent/span 文本 + Langfuse 深链。

    当 _emit_step_event 和 _task_id 传入时，每完成一个步骤的评分就立即推送
    scenario_step_done 事件（实现增量进度展示）。
    """
    scenario_key = str(tpl_data.get("scenario_key") or "")
    fa_steps = _final_answer_steps_for_scenario(scenario_key, tpl_data)
    observations, obs_source = await load_observations_for_run(session, run)
    if not skip_llm_judge:
        # 统计各 step_index 对应的 observation，辅助排查步骤 4 绑定问题
        # 排除容器 span（tool_batch / iteration / agent_loop），只统计可匹配的叶子节点
        step_index_map: Dict[int, list] = {}
        for obs in observations:
            if _is_container_observation(obs):
                continue
            si = _observation_step_index(obs)
            if si is not None:
                step_index_map.setdefault(si, []).append({
                    "name": obs.get("name"),
                    "type": obs.get("type"),
                    "id": obs.get("id"),
                    "output_len": len(str(obs.get("output") or "")),
                    "iteration": _observation_iteration(obs),
                })
        log(
            "INFO",
            "SCENARIO_STEP",
            "加载观察数据",
            run_id=run.id,
            obs_source=obs_source,
            obs_count=len(observations),
            scenario_key=scenario_key,
            agent_answer_len=len(agent_answer or ""),
            step_index_distribution={
                str(k): len(v) for k, v in step_index_map.items()
            },
            step_index_detail={
                str(k): v for k, v in step_index_map.items()
            },
        )
    run_links = await build_langfuse_links_for_run(session, run)
    project_id = run_links.get("langfuse_project_id")
    # 查询任务级别的评估模型和本体包，用于场景步骤 LLM 评判 fallback 和证据集评分
    task_llm_model: Optional[str] = None
    task_ontology_package: Optional[str] = None
    if getattr(run, "task_id", None):
        task_row = await session.get(EvaluationTask, run.task_id)
        if task_row:
            if getattr(task_row, "llm_model", None):
                task_llm_model = task_row.llm_model
            if getattr(task_row, "ontology_package", None):
                task_ontology_package = task_row.ontology_package

    # 构建证据集映射（如果有本体包）
    ontology_workflow = None
    step_evidence_map: Optional[Dict[str, List[str]]] = None

    if task_ontology_package:
        try:
            from pathlib import Path
            from services.ontology_workflow_parser import parse_ontology_workflow

            # 获取本体包目录
            import os
            ontology_base_dir = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "md"
            ontology_dir = ontology_base_dir / task_ontology_package

            if ontology_dir.exists():
                ontology_workflow = parse_ontology_workflow(ontology_dir)
                if ontology_workflow:
                    step_evidence_map = build_evidence_map_from_trace(
                        ontology_workflow, observations, raw_steps
                    )
                    log(
                        "INFO",
                        "EVIDENCE_SET",
                        "Built evidence map from ontology workflow",
                        task_ontology_package=task_ontology_package,
                        operations_count=len(ontology_workflow.operations),
                        mapped_steps_count=len(step_evidence_map),
                    )
            else:
                log("WARNING", "EVIDENCE_SET", "Ontology directory not found",
                    expected_path=str(ontology_dir))
        except Exception as exc:
            log("WARNING", "EVIDENCE_SET",
                "Failed to build evidence map, falling back to heuristic",
                error=str(exc)[:200])
            ontology_workflow = None
            step_evidence_map = None

    step_results: List[Dict[str, Any]] = []
    composite = 0.0
    weight_sum = 0.0

    # 从文件加载标准答案 / 评测提示词（文件优先于 DB）
    dataset_id = str(tpl_data.get("dataset_id") or "")
    from services.step_asset_service import (
        load_asset,
        KIND_GROUND_TRUTH,
        KIND_LLM_PROMPT,
    )

    def _resolve_step_assets(s: Dict[str, Any]) -> None:
        """如果文件存在，用文件内容覆盖 DB 中的 gt_expected 和 aggregate_prompt"""
        sk = str(s.get("step_key") or "")
        if not dataset_id or not sk:
            return
        # 标准答案
        gt_file = load_asset(dataset_id, scenario_key, sk, KIND_GROUND_TRUTH)
        if gt_file is not None:
            gt = s.get("gt_expected")
            if isinstance(gt, dict):
                gt = dict(gt)
            else:
                gt = {}
            gt["keywords"] = [k.strip() for k in gt_file.split(",") if k.strip()]
            s["gt_expected"] = gt
        # 评测提示词
        prompt_file = load_asset(dataset_id, scenario_key, sk, KIND_LLM_PROMPT)
        if prompt_file is not None:
            mr = s.get("match_rules")
            if not isinstance(mr, dict):
                mr = {}
            else:
                mr = dict(mr)
            ec = mr.get("eval_config")
            if not isinstance(ec, dict):
                ec = {}
            else:
                ec = dict(ec)
            ec["aggregate_prompt"] = prompt_file.strip()
            # 有评测提示词文件时，模式自动设为 llm_aggregate（除非已明确设置为其他值）
            if not ec.get("mode") or ec.get("mode") == "keyword":
                ec["mode"] = "llm_aggregate"
            mr["eval_config"] = ec
            s["match_rules"] = mr

    raw_steps = tpl_data.get("steps") or []
    active_steps = sorted(
        [s for s in raw_steps if s.get("enabled", True)],
        key=lambda s: (
            int(s.get("step_order") or 0),
            str(s.get("step_key") or ""),
        ),
    )

    # 预处理：对所有 active_steps 解析 assets + binding，收集每步最终 output_keywords，
    # 供 bind_step_observation 计算 cross_step_hits（跨步骤命中数）。
    # _resolve_step_assets 是幂等的（从文件加载），预处理不影响循环内再次调用。
    # llm_aggregate 模式会追加 JSON 结构关键词（render_datas 等），也需纳入 peer 关键词，
    # 否则该模式步骤的 span 不会被他步计入 mo，削弱纯度 tiebreaker 的区分力。
    peer_keywords_map: Dict[str, List[str]] = {}
    for step in active_steps:
        _resolve_step_assets(step)
        sk = str(step.get("step_key") or "")
        if not sk:
            continue
        ec = parse_eval_config(step.get("match_rules"))
        b = get_step_trace_binding(scenario_key, sk, step)
        kws = list(b.get("output_keywords") or [])
        if ec["mode"] == "llm_aggregate":
            for jk in ("render_datas", "tableData", "draw_info", "tableType"):
                if jk not in kws:
                    kws.append(jk)
        peer_keywords_map[sk] = kws

    for step_idx, step in enumerate(active_steps):
        _resolve_step_assets(step)
        step_key = step.get("step_key") or ""

        # 根据评测模式动态调整 span 绑定策略
        eval_config = parse_eval_config(step.get("match_rules"))
        binding = get_step_trace_binding(scenario_key, step_key, step)

        # 注意：不再自动用 step_idx 设置 expected_step_index。
        # step_idx 是场景模板步骤的 0-based 序号，而 observation.step_index 是
        # span 树 DFS 遍历序号（run_span_nodes 路径，见 span_tree_builder.walk）
        # 或 agent 自身执行序号（langfuse_api 路径）—— 二者无契约对齐关系，
        # agent 不感知场景模板。自动设值会导致 step_index 精确匹配绑到无关 span。
        # 仅当步骤配置 match_rules.trace_binding.expected_step_index 显式声明时才启用。

        if eval_config["mode"] == "llm_aggregate":
            # 基于Prompt评测模式：优先匹配 tool span（通常包含结构化 JSON 输出）
            # 策略：清空原有自然语言关键词，注入 JSON 结构关键词使 tool span 天然得分更高
            patterns = list(binding.get("span_name_patterns") or [])
            if "tool:" not in patterns:
                patterns.insert(0, "tool:")
            elif patterns[0] != "tool:":
                patterns.remove("tool:")
                patterns.insert(0, "tool:")
            binding["span_name_patterns"] = patterns
            # 追加 JSON 结构关键词（保留原始 output_keywords，兼容自然语言输出）。
            # 原逻辑直接替换会丢失 step 配置的中文关键词（如"全流程执行汇总"），
            # 导致 agent_swarm 自然语言输出（非 JSON）keyword 得分恒为 0。
            orig_keywords = list(binding.get("output_keywords") or [])
            for jk in ("render_datas", "tableData", "draw_info", "tableType"):
                if jk not in orig_keywords:
                    orig_keywords.append(jk)
            binding["output_keywords"] = orig_keywords

        used_obs_ids: set = set()
        for prev in step_results:
            prev_oid = (prev.get("trace_binding") or {}).get("observation_id")
            if prev_oid:
                used_obs_ids.add(prev_oid)

        # peer_step_keywords：同场景其他步骤的 output_keywords（不含本步骤），
        # 用于 bind_step_observation 计算 cross_step_hits（跨步骤命中数），
        # 在 own score 平局时作 tiebreaker 抑制跨步骤混合 span。
        peer_kws = {
            osk: okws
            for osk, okws in peer_keywords_map.items()
            if osk != step_key
        }

        # ★ 证据集评分路径（如果本体包解析成功且该步骤有映射）
        use_evidence_set = (
            step_evidence_map is not None
            and step_key in step_evidence_map
        )

        if use_evidence_set:
            # 收集所有 evidence observations 的输出
            evidence_obs_ids = step_evidence_map[step_key]
            evidence_obs_id_set = set(evidence_obs_ids)
            merged_parts: List[str] = []

            for obs_item in observations:
                obs_id = str(obs_item.get("id") or "")
                if obs_id in evidence_obs_id_set:
                    blob = _observation_output_blob(obs_item)
                    if blob:
                        merged_parts.append(blob)

            if merged_parts:
                # 合并所有 evidence 文本
                merged_text = "\n\n".join(merged_parts)
                # 限制合并文本长度（防止内存溢出）
                max_evidence_len = 50000
                if len(merged_text) > max_evidence_len:
                    merged_text = merged_text[:max_evidence_len]
                    log("WARNING", "EVIDENCE_SET",
                        "Merged evidence text truncated",
                        step_key=step_key,
                        original_len=len(merged_text),
                        truncated_len=max_evidence_len)

                # 构造 bind_meta 和 io
                bind_meta = {
                    "bind_source": "evidence_set",
                    "step_key": step_key,
                    "evidence_observation_ids": evidence_obs_ids,
                    "evidence_count": len(merged_parts),
                    "observation_id": evidence_obs_ids[0] if evidence_obs_ids else None,
                }
                io = {
                    "input_preview": "",
                    "output_preview": merged_text,
                    "observation_id": evidence_obs_ids[0] if evidence_obs_ids else None,
                    "observation_name": "evidence_set_merged",
                    "observation_type": "MERGED",
                }
                score_text, score_source = merged_text, "evidence_set"

                log("INFO", "EVIDENCE_SET", "Using evidence-set scoring",
                    step_key=step_key,
                    evidence_count=len(merged_parts),
                    merged_text_len=len(merged_text))
            else:
                # 有映射但无有效输出，降级到 heuristic
                use_evidence_set = False

        if not use_evidence_set:
            # ★ 原有 heuristic 评分路径
            obs, bind_meta = bind_step_observation(
                step_key,
                observations,
                binding=binding,
                final_answer_text=agent_answer,
                final_answer_steps=fa_steps,
                used_observation_ids=used_obs_ids,
                peer_step_keywords=peer_kws,
            )
            io = extract_observation_io(obs)
            score_text, score_source = _resolve_step_score_text(
                step_key, io, bind_meta, agent_answer, final_answer_steps=fa_steps
            )

        # ★ 基于Prompt评测分支（直接评判，不再二次汇聚）
        llm_judge_reason: Optional[str] = None
        evidence_note: Optional[str] = None
        llm_judge_used = False

        if eval_config["mode"] == "llm_aggregate":
            if not eval_config["aggregate_prompt"]:
                evidence_note = "基于Prompt评测配置不完整（缺少Prompt）"
                score_source = "llm_aggregate_config_invalid"
                score, passed, span_ev = 0.0, False, {"reason": evidence_note}
            elif not score_text:
                # 无 Agent 输出，LLM 无法评测
                score_source = "llm_aggregate_no_input"
                score, passed, span_ev = 0.0, False, {"reason": "Agent 无输出，无法进行基于Prompt评测"}
            elif skip_llm_judge:
                # 跳过基于Prompt评测（如已有持久化结果时）
                score_source = "llm_aggregate_skipped"
                score, passed, span_ev = _score_step(score_text, step.get("gt_expected"))
            else:
                # 基于Prompt评测：使用用户Prompt评测，标准答案仅作参考
                effective_llm_model = _resolve_effective_llm_model(
                    eval_config, run, task_llm_model
                )
                if is_llm_configured(effective_llm_model):
                    try:
                        score, passed, span_ev = await llm_judge_step_output(
                            step_output=score_text,
                            aggregate_prompt=eval_config["aggregate_prompt"],
                            gt_expected=step.get("gt_expected"),
                            llm_model=effective_llm_model,
                        )
                        score_source = "llm_judge"
                        llm_judge_used = True
                        llm_judge_reason = span_ev.get("reason", "")
                    except Exception as exc:

                        log(
                            "WARNING",
                            "SCENARIO_LLM_AGG",
                            "基于Prompt评测失败",
                            step_key=step_key,
                            error=str(exc),
                        )
                        # 基于Prompt评测模式下不回退关键词判分，直接给 0 分
                        score_source = "llm_judge_failed"
                        evidence_note = f"基于Prompt评测失败（{exc}）"
                        score, passed, span_ev = 0.0, False, {"reason": evidence_note}
                else:
                    evidence_note = "未配置 LLM API Key，无法进行基于Prompt评测"
                    score_source = "llm_aggregate_skipped"
                    score, passed, span_ev = 0.0, False, {"reason": evidence_note}
        else:
            # Ground_Truth评测模式：使用标准答案关键词评测
            score, passed, span_ev = _score_step(score_text, step.get("gt_expected"))
            # keyword 评分过低且有参考答案时，自动降级到 LLM 语义评测
            # 避免 keyword 模式对复杂步骤的系统性误判（如具体数值格式差异导致 0 命中）
            if (
                not skip_llm_judge
                and score < 0.5
                and not passed
                and score_text
            ):
                gt_exp = step.get("gt_expected") or {}
                gt_text = (gt_exp.get("text") or "").strip()
                if gt_text:
                    effective_llm_model = _resolve_effective_llm_model(
                        eval_config, run, task_llm_model
                    )
                    if is_llm_configured(effective_llm_model):
                        from services.scenario_model_answer_draft_service import (
                            _auto_generate_aggregate_prompt,
                        )
                        try:
                            downgrade_prompt = _auto_generate_aggregate_prompt(
                                step.get("name") or step_key,
                                gt_text,
                                step.get("gt_criteria") or [],
                                gt_exp.get("keywords") or [],
                            )
                            llm_score, llm_passed, llm_ev = await llm_judge_step_output(
                                step_output=score_text,
                                aggregate_prompt=downgrade_prompt,
                                gt_expected=gt_exp,
                                llm_model=effective_llm_model,
                            )
                            keyword_score_orig = score
                            score_source = "keyword_downgrade_llm"
                            score, passed, span_ev = llm_score, llm_passed, llm_ev
                            llm_judge_used = True
                            llm_judge_reason = llm_ev.get("reason", "")
                            evidence_note = (
                                f"keyword 评分 {llm_score:.2f}（原 {keyword_score_orig:.2f}），"
                                f"已自动降级到 LLM 语义评测"
                            )
                        except Exception as exc:
                            log(
                                "WARNING",
                                "SCENARIO_KEYWORD_DOWNGRADE",
                                "keyword 自动降级 LLM 失败，保留原 keyword 评分",
                                step_key=step_key,
                                error=str(exc)[:200],
                            )

        # 诊断日志：仅在真正执行评测时打印（跳过查询时的缓存读取）
        if not skip_llm_judge:
            log(
                "INFO",
                "SCENARIO_STEP",
                "步骤评测结果",
                step_key=step_key,
                step_name=step.get("name"),
                step_index=step_idx,
                eval_mode=eval_config["mode"],
                has_aggregate_prompt=bool(eval_config.get("aggregate_prompt")),
                aggregate_prompt=eval_config.get("aggregate_prompt", ""),
                score_text_len=len(score_text or ""),
                bind_source=bind_meta.get("bind_source"),
                bind_observation_id=io.get("observation_id"),
                bind_observation_name=io.get("observation_name"),
                bind_observation_type=io.get("observation_type"),
                output_preview_len=len(io.get("output_preview") or ""),
                obs_source=obs_source,
                result_score_source=score_source,
                score=score,
                passed=passed,
                evidence_note=evidence_note,
            )
        supplement_steps = fa_steps if is_per_item_scenario_key(scenario_key) else frozenset()
        if (
            eval_config["mode"] != "llm_aggregate"
            and step_key in supplement_steps
            and agent_answer
            and not passed
            and bind_meta.get("bind_source")
            in ("heuristic", "heuristic_no_keyword", "agent_step_id", "evidence_set")
        ):
            merged = f"{score_text}\n{agent_answer}".strip()
            score2, passed2, ev2 = _score_step(merged, step.get("gt_expected"))
            if score2 > score or (passed2 and not passed):
                score, passed, span_ev = score2, passed2, ev2
                score_source = "span_plus_final_answer"
                evidence_note = (
                    "绑定 span 未满分，已合并终局答案补全关键词（完整方案常在较晚 iteration）"
                )
        elif (
            eval_config["mode"] == "llm_aggregate"
            and not skip_llm_judge
            and agent_answer
            and not passed
            and bind_meta.get("bind_source")
            in ("heuristic", "heuristic_no_keyword", "agent_step_id", "evidence_set")
        ):
            # llm_aggregate 补救：LLM judge 判 not passed 时，合并终局答案重判。
            # onto-agent 中间步骤 evidence 常跨 span，绑定 span 的 output 可能不含
            # 实质内容（仅 reasoning_content 含关键词命中），但终局答案汇总了所有步骤结果。
            effective_llm_model = _resolve_effective_llm_model(
                eval_config, run, task_llm_model
            )
            if is_llm_configured(effective_llm_model):
                merged = f"{score_text}\n{agent_answer}".strip()
                try:
                    score2, passed2, ev2 = await llm_judge_step_output(
                        step_output=merged,
                        aggregate_prompt=eval_config["aggregate_prompt"],
                        gt_expected=step.get("gt_expected"),
                        llm_model=effective_llm_model,
                    )
                    if score2 > score or (passed2 and not passed):
                        score, passed, span_ev = score2, passed2, ev2
                        score_source = "llm_judge_supplement"
                        llm_judge_reason = ev2.get("reason", "")
                        evidence_note = (
                            "绑定 span LLM 评测未过，已合并终局答案重判"
                            "（完整方案常在较晚 iteration 或跨 span 汇总）"
                        )
                except Exception as exc:
                    log(
                        "WARNING",
                        "SCENARIO_LLM_AGG_SUPPLEMENT",
                        "llm_aggregate 补救重判失败，保留原 LLM judge 分数",
                        step_key=step_key,
                        error=str(exc)[:200],
                    )
        evidence = {
            **bind_meta,
            **span_ev,
            "score_source": score_source,
            "observation_source": obs_source,
        }
        if evidence_note:
            evidence["note"] = evidence_note
        if bind_meta.get("bind_source") == "heuristic_no_keyword":
            evidence["note"] = "已绑定 span，但 output 未命中本子步骤关键词"
        elif bind_meta.get("bind_source") == "final_answer_no_span":
            evidence["note"] = "span 候选均未命中关键词（并行处理 evidence 跨 span），已用终局答案评测"
        elif bind_meta.get("bind_source") == "unbound" and step_key not in fa_steps:
            evidence["note"] = "未绑定 span，且非汇总步骤，不使用终局答案判分"

        if eval_config["mode"] == "llm_aggregate":
            evidence["eval_mode"] = "llm_judge"
            evidence["aggregate_prompt"] = eval_config.get("aggregate_prompt", "")
            if llm_judge_reason:
                evidence["llm_judge_reason"] = llm_judge_reason[:200]
        else:
            evidence["eval_mode"] = "keyword"

        # obs=None（final_answer_no_span）时 io.observation_id 为 None，
        # 从 bind_meta 补充 best span 的 observation_id，供 trace_binding 记录、
        # step_links 生成深链、以及 used_obs_ids 去重（避免后续步骤重复绑定）。
        obs_id = io.get("observation_id") or bind_meta.get("observation_id")
        step_links = build_trace_urls_sync(
            trace_id=str(run_links.get("trace_id") or ""),
            langfuse_project_id=project_id,
            observation_id=str(obs_id) if obs_id else None,
            host=run_links.get("langfuse_host"),
        )
        trace_binding = {
            "label": binding.get("label"),
            "observation_source": obs_source,
            **io,
            **step_links,
            "langfuse_project_key": run_links.get("langfuse_project_key"),
        }
        # final_answer_no_span 时 io 的 observation_id/name 为 None，用 bind_meta 补充
        if not trace_binding.get("observation_id") and bind_meta.get("observation_id"):
            trace_binding["observation_id"] = bind_meta["observation_id"]
        if not trace_binding.get("observation_name") and bind_meta.get("observation_name"):
            trace_binding["observation_name"] = bind_meta["observation_name"]
        if trace_binding.get("trace_url"):
            evidence["langfuse_trace_url"] = trace_binding["trace_url"]
        if trace_binding.get("observation_url") and obs_id:
            evidence["langfuse_observation_url"] = trace_binding["observation_url"]

        weight = float(step["weight"])
        weight_sum += weight
        composite += score * weight
        step_results.append(
            {
                "step_id": step["id"],
                "step_key": step_key,
                "name": step["name"],
                "step_order": step.get("step_order"),
                "weight": step["weight"],
                "required": step["required"],
                "enabled": step.get("enabled", True),
                "enforce_order": bool(step.get("enforce_order", False)),
                "gt_criteria": step.get("gt_criteria"),
                "score": score,
                "passed": passed,
                "weighted_score": round(score * weight, 4),
                "evidence": evidence,
                "trace_binding": trace_binding,
            }
        )

        # 增量发送场景步骤完成事件（LLM 评分完成后立即推送）
        if _emit_step_event and _task_id and _current_step_ref is not None:
            _current_step_ref[0] += 1
            await _emit_step_event(_task_id, "scenario_step_done", {
                "step_index": _current_step_ref[0],
                "total_steps": _total_steps or len(active_steps),
                "step_key": step_key,
                "step_name": step.get("name"),
                "question_id": getattr(run, "question_id", None),
                "score": score,
                "passed": passed,
                "source": "realtime",
            })

    _apply_scenario_enforce_order(observations, step_results)

    if persist_span_refs:
        from services.scenario_step_span_ref_service import persist_scenario_step_span_refs

        await persist_scenario_step_span_refs(session, run.id, step_results)

    if weight_sum > 0:
        composite = composite / weight_sum

    return {
        "steps": step_results,
        "composite_score": round(composite, 4),
        "weight_sum": round(weight_sum, 4),
        "observation_source": obs_source,
        "langfuse": run_links,
    }


async def build_run_scenario_steps_detail(
    session: AsyncSession,
    run_id: str,
    scenario_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """单 Run 场景子步骤：打分 + 绑定 span input/output + Langfuse 深链。"""
    from services.scenario_eval_service import resolve_scenario_for_run
    from services.scenario_template_utils import template_with_active_steps_only

    run = await session.get(AgentRun, run_id)
    if not run:
        return None
    # 场景子步骤维度关闭时，不触发实时补算（避免 Pipeline 跳过后又被接口回填）
    if run.task_id:
        _task = await session.get(EvaluationTask, run.task_id)
        if _task and not getattr(_task, "auto_scenario_score", True):
            return None

    resolved_key = (scenario_key or "").strip() or await resolve_scenario_for_run(
        session, run_id
    )
    if not resolved_key:
        return None

    tpl_raw = await get_template_by_key(session, resolved_key)
    if not tpl_raw:
        return None
    tpl_data = template_with_active_steps_only(tpl_raw)
    if not tpl_data.get("steps"):
        return None

    from database import EvaluationResult

    eval_row = (
        await session.execute(
            select(EvaluationResult)
            .where(EvaluationResult.agent_run_id == run.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    agent_answer = (eval_row.agent_answer if eval_row else "") or ""

    # 优先读取已持久化的打分结果，避免 LLM 重复调用导致评分不一致
    from database import ScenarioStepResult
    persisted = (
        await session.execute(
            select(ScenarioStepResult)
            .where(ScenarioStepResult.agent_run_id == run.id)
        )
    ).scalars().all()
    if persisted:
        # 有持久化结果时跳过 LLM 调用，仅计算 trace 绑定信息
        result = await compute_run_scenario_steps(
            session, run, tpl_data, agent_answer,
            persist_span_refs=False, skip_llm_judge=True,
        )
        # 用已持久化的分数和 evidence 覆盖实时计算结果
        persisted_map = {r.step_id: r for r in persisted}
        for step_res in result.get("steps", []):
            step_id = step_res.get("step_id") or ""
            pr = persisted_map.get(step_id)
            if pr:
                step_res["score"] = pr.score
                step_res["passed"] = pr.passed
                if pr.evidence:
                    step_res["evidence"] = pr.evidence
                # 懒回填 trace_binding：旧数据（迁移 027 前）trace_binding 为 NULL，
                # 用户首次打开 steps-detail 抽屉时把 compute_run_scenario_steps 计算出的
                # trace_binding 写回 DB。仅 UPDATE NULL 行，不动 score/evidence，
                # 不触发 DELETE+INSERT。后续矩阵快路径即可直接返回真实绑定数据。
                tb = step_res.get("trace_binding")
                if tb and not pr.trace_binding:
                    pr.trace_binding = tb
        await session.flush()
        # 持久化分数覆盖后重算 composite 与 weighted_score：
        # compute_run_scenario_steps(skip_llm_judge=True) 返回的 composite
        # 仍是 keyword 模式的陈旧值，与 matrix 接口的 5 轮均值不一致。
        weight_sum = 0.0
        composite = 0.0
        for step_res in result.get("steps", []):
            weight = float(step_res.get("weight") or 0.0)
            score = float(step_res.get("score") or 0.0)
            weight_sum += weight
            composite += score * weight
            step_res["weighted_score"] = round(score * weight, 4)
        if weight_sum > 0:
            composite = composite / weight_sum
        result["composite_score"] = round(composite, 4)
        result["weight_sum"] = round(weight_sum, 4)
    else:
        result = await compute_run_scenario_steps(session, run, tpl_data, agent_answer)
    from services.scenario_step_span_ref_service import list_scenario_step_span_refs

    span_refs = await list_scenario_step_span_refs(session, run.id)
    return {
        "agent_run_id": run.id,
        "scenario_key": resolved_key,
        "template": tpl_data,
        "composite_score": result["composite_score"],
        "trace_id": result["langfuse"].get("trace_id"),
        "langfuse_host": result["langfuse"].get("langfuse_host"),
        "langfuse": result["langfuse"],
        "observation_source": result["observation_source"],
        "steps": result["steps"],
        "span_refs": span_refs,
    }
