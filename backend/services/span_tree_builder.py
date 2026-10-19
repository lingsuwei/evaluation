"""
Langfuse trace observations → run_span_nodes Span Tree（P2 C-02）。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from config import BUILDER_ALLOW_LEGACY_AGGREGATE_KEY
from services.observation_latency import (
    parse_observation_datetime,
    resolve_observation_latency_ms,
)
from services.otel_attributes import (
    derive_aggregate_key,
    normalize_observation_attributes,
)
from services.otel_mapping_engine import create_mapping_engine


def _parent_obs_id(obs: Dict[str, Any]) -> Optional[str]:
    for key in ("parentObservationId", "parent_observation_id", "parentId", "parent_id"):
        val = obs.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def _obs_id(obs: Dict[str, Any]) -> Optional[str]:
    val = obs.get("id")
    return str(val).strip() if val is not None and str(val).strip() else None


def _tokens(obs: Dict[str, Any]) -> Optional[int]:
    usage = obs.get("usage")
    if not isinstance(usage, dict):
        return None
    for key in ("total", "totalTokens", "total_tokens", "totalTokenCount", "total_token_count"):
        try:
            if usage.get(key) is not None:
                return int(usage[key])
        except (TypeError, ValueError):
            continue
    try:
        prompt = (
            usage.get("promptTokens")
            or usage.get("prompt_tokens")
            or usage.get("inputTokens")
            or usage.get("input_tokens")
            or usage.get("input")
            or 0
        )
        completion = (
            usage.get("completionTokens")
            or usage.get("completion_tokens")
            or usage.get("outputTokens")
            or usage.get("output_tokens")
            or usage.get("output")
            or 0
        )
        if prompt or completion:
            return int(prompt or 0) + int(completion or 0)
    except (TypeError, ValueError):
        return None
    return None


def _infer_semantic_role(obs_type: Optional[str], attributes: Dict[str, Any]) -> Optional[str]:
    kind = attributes.get("agent.step.kind")
    if kind is not None and str(kind).strip():
        return str(kind).strip()
    t = (obs_type or "").lower()
    if t in ("generation", "llm"):
        return "llm"
    if t in ("tool", "tool_call"):
        return "tool"
    if t in ("span", "event"):
        return "span"
    return None


def _summary_text(obs: Dict[str, Any], *, max_len: int = 8192) -> Optional[str]:
    for key in ("output", "completion", "answer", "text"):
        val = obs.get(key)
        if val is None:
            continue
        if isinstance(val, str):
            s = val.strip()
        else:
            # 非字符串值用 json.dumps 产出合法 JSON（双引号），便于前端格式化
            try:
                s = json.dumps(val, ensure_ascii=False)
            except (TypeError, ValueError):
                s = str(val).strip()
        if s:
            return s if len(s) <= max_len else s[: max_len - 1] + "…"
    return None


def _summary_input(obs: Dict[str, Any], *, max_len: int = 1024) -> Optional[str]:
    """提取 observation 的 input，多源 fallback（input → metadata.input），截断到 max_len。"""
    val = obs.get("input")
    if val is None:
        meta = obs.get("metadata") or {}
        if isinstance(meta, dict):
            val = meta.get("input")
    if val is None:
        return None
    if isinstance(val, str):
        s = val.strip()
    else:
        try:
            s = json.dumps(val, ensure_ascii=False)
        except (TypeError, ValueError):
            s = str(val)
    if not s:
        return None
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def _coerce_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    if isinstance(val, (int, float)):
        return bool(val)
    return False


def _coerce_mock_payload(val: Any) -> Optional[Dict[str, Any]]:
    """解析 agent.mock_response_payload（object 或 JSON 字符串）。"""
    if val is None:
        return None
    if isinstance(val, dict):
        return val
    if isinstance(val, str) and val.strip():
        import json

        try:
            parsed = json.loads(val)
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}
        except json.JSONDecodeError:
            return {"text": val.strip()}
    return None


def _extract_tool_calls_from_output(obs: Dict[str, Any]) -> Tuple[int, List[str]]:
    """从 observation.output 解析 tool_calls（OpenAI 消息格式）。

    onto-agent 的工具调用不作为独立 observation，而是嵌在 GENERATION 的
    output.tool_calls 里。兼容两种结构：
      - output.tool_calls[i].function.name
      - output.choices[i].message.tool_calls[j].function.name

    Returns: (tool_call_count, [tool_name, ...])；解析失败返回 (0, [])。
    """
    raw = obs.get("output")
    if raw is None:
        return 0, []
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return 0, []
        try:
            raw = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return 0, []
    if not isinstance(raw, dict):
        return 0, []

    call_lists: List[Any] = []
    top_tc = raw.get("tool_calls")
    if isinstance(top_tc, list):
        call_lists.append(top_tc)
    choices = raw.get("choices")
    if isinstance(choices, list):
        for ch in choices:
            if not isinstance(ch, dict):
                continue
            msg = ch.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("tool_calls"), list):
                call_lists.append(msg["tool_calls"])

    names: List[str] = []
    for tc_list in call_lists:
        for tc in tc_list:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            if isinstance(fn, dict) and fn.get("name"):
                names.append(str(fn["name"]))
            elif tc.get("name"):
                names.append(str(tc["name"]))
    return len(names), names


def _read_metadata_value(obs: Dict[str, Any], key: str) -> Any:
    meta = obs.get("metadata")
    if isinstance(meta, dict):
        return meta.get(key)
    return None


def build_span_node_rows(
    trace: Dict[str, Any],
    agent_run_id: str,
    *,
    allow_legacy_aggregate_key: Optional[bool] = None,
    provider: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    将 trace.observations 转为 run_span_nodes 行（未落库）。
    返回 dict 列表，含 parent_id 为平台侧 span id（非 observation_id）。

    provider: Provider 名称（如 agent_swarm），用于 OTel 属性映射。
              None 表示无映射（onto-agent 已自写规范属性）。
    """
    observations = trace.get("observations") or []
    if not isinstance(observations, list):
        return []

    allow_legacy = (
        BUILDER_ALLOW_LEGACY_AGGREGATE_KEY
        if allow_legacy_aggregate_key is None
        else allow_legacy_aggregate_key
    )

    # OTel 属性映射引擎（Provider 级 YAML 配置，agent_swarm 存量观测映射）
    mapping_engine = create_mapping_engine(provider)

    obs_by_id: Dict[str, Dict[str, Any]] = {}
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        oid = _obs_id(obs)
        if oid:
            obs_by_id[oid] = obs

    # observation_id -> platform node id
    obs_to_node: Dict[str, str] = {}
    rows: List[Dict[str, Any]] = []

    for obs in observations:
        if not isinstance(obs, dict):
            continue
        oid = _obs_id(obs)
        if not oid:
            continue
        node_id = f"span_{uuid.uuid4().hex[:12]}"
        obs_to_node[oid] = node_id

        attrs = normalize_observation_attributes(obs)
        if mapping_engine:
            attrs = mapping_engine.apply(obs, attrs)
        # 提取 input/output 文本存入 attributes（供 span-tree API 返回）
        input_text = _summary_input(obs)
        if not input_text:
            input_text = attrs.get("message")
        if not input_text:
            input_text = attrs.get("messages_preview")
        if input_text:
            attrs["agent.io.input"] = input_text
        obs_type = obs.get("type")
        if obs_type is not None:
            obs_type = str(obs_type)

        # GENERATION 的工具调用嵌在 output.tool_calls（OpenAI 消息格式），
        # 不是独立 observation；提取计数与名称供运行时统计复用。
        tc_count, tc_names = _extract_tool_calls_from_output(obs)
        if tc_count:
            attrs["agent.tool.call_count"] = tc_count
            attrs["agent.tool.names"] = tc_names

        raw_det = _read_metadata_value(obs, "agent.is_deterministic_io")
        if raw_det is None:
            raw_det = attrs.get("agent.is_deterministic_io")
        raw_mock = _read_metadata_value(obs, "agent.mock_response_payload")
        if raw_mock is None:
            raw_mock = attrs.get("agent.mock_response_payload")
        is_det = _coerce_bool(raw_det) if raw_det is not None else False
        mock_payload = _coerce_mock_payload(raw_mock)
        if is_det and mock_payload is None and raw_mock is not None:
            is_det, mock_payload = True, None

        rows.append(
            {
                "id": node_id,
                "agent_run_id": agent_run_id,
                "parent_id": None,  # filled below
                "_parent_obs_id": _parent_obs_id(obs),
                "observation_id": oid,
                "node_type": obs_type.lower() if obs_type else None,
                "name": obs.get("name"),
                "attributes": attrs,
                "aggregate_key": derive_aggregate_key(
                    attrs,
                    observation_type=obs_type,
                    observation_name=obs.get("name"),
                    allow_legacy_type_name=allow_legacy,
                ),
                "semantic_role": _infer_semantic_role(obs_type, attrs),
                "summary_text": _summary_text(obs),
                "is_deterministic_io": is_det,
                "mock_response_payload": mock_payload,
                "status": obs.get("status"),
                "latency_ms": resolve_observation_latency_ms(obs),
                "tokens": _tokens(obs),
                "started_at": parse_observation_datetime(
                    obs.get("startTime") or obs.get("start_time")
                ),
                "ended_at": parse_observation_datetime(
                    obs.get("endTime") or obs.get("end_time")
                ),
            }
        )

    for row in rows:
        parent_obs = row.pop("_parent_obs_id", None)
        if parent_obs and parent_obs in obs_to_node:
            row["parent_id"] = obs_to_node[parent_obs]
        else:
            row["parent_id"] = None

    row_by_id = {r["id"]: r for r in rows}
    children: Dict[Optional[str], List[str]] = {}
    for row in rows:
        children.setdefault(row["parent_id"], []).append(row["id"])

    for pid in children:
        children[pid].sort(
            key=lambda nid: row_by_id[nid]["observation_id"],
        )

    step_counter = 0

    def walk(node_id: Optional[str], depth: int) -> None:
        nonlocal step_counter
        for idx, child_id in enumerate(children.get(node_id, [])):
            row = row_by_id[child_id]
            row["depth"] = depth
            row["sibling_order"] = idx
            row["step_index"] = step_counter
            step_counter += 1
            walk(child_id, depth + 1)

    walk(None, 0)
    return rows


def span_attributes_complete(node_rows: List[Dict[str, Any]]) -> bool:
    """C-07：至少一个 span 含 agent.step.id 视为属性完整。"""
    for row in node_rows:
        attrs = row.get("attributes")
        if not isinstance(attrs, dict):
            continue
        step_id = attrs.get("agent.step.id")
        if step_id is not None and str(step_id).strip():
            return True
    return False
