"""
Langfuse observation → OTel 风格 attributes 归一化（v1.7 架构 §8.8）。

过程点 JSONPath、Structural 聚合应只读 normalize 后的字典，而非 observation.name。
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


AGENT_PREFIX = "agent."
EVAL_PREFIX = "eval."


def normalize_observation_attributes(obs: Mapping[str, Any]) -> Dict[str, Any]:
    """合并 observation 字段与 metadata，产出稳定 attributes 字典。"""
    out: Dict[str, Any] = {}
    meta = obs.get("metadata")
    if isinstance(meta, dict):
        for k, v in meta.items():
            if v is None:
                continue
            out[str(k)] = _coerce_value(v)

    # Langfuse 常用域字段 → agent.*（若 metadata 未覆盖）
    for src, dst in (
        ("logical_agent_id", "agent.logical_agent_id"),
        ("agent_version", "agent.version"),
        ("version", "agent.version"),
    ):
        if dst not in out and obs.get(src) is not None:
            out[dst] = _coerce_value(obs[src])

    return out


def nest_agent_attribute_keys(flat: Mapping[str, Any]) -> Dict[str, Any]:
    """
    将 `agent.step.id` / `eval.operation.id` 等扁平键嵌套为树结构，
    以便 JSONPath `$.attributes.agent.step.id` / `$.attributes.eval.operation.id` 求值（OTel 常用点号键名）。
    """
    tree: Dict[str, Any] = {}
    for k, v in flat.items():
        if isinstance(k, str) and k.startswith((AGENT_PREFIX, EVAL_PREFIX)) and "." in k:
            parts = k.split(".")
            cur: Dict[str, Any] = tree
            for p in parts[:-1]:
                nxt = cur.get(p)
                if not isinstance(nxt, dict):
                    nxt = {}
                    cur[p] = nxt
                cur = nxt
            cur[parts[-1]] = v
        else:
            tree[str(k)] = v
    return tree


def build_match_document(obs: Mapping[str, Any]) -> Dict[str, Any]:
    """过程点 / JSONPath 求值用的统一文档根。"""
    flat = normalize_observation_attributes(obs)
    return {
        "attributes": nest_agent_attribute_keys(flat),
        "attributes_flat": dict(flat),
        "name": obs.get("name"),
        "type": obs.get("type"),
        "id": obs.get("id"),
    }


def derive_aggregate_key(
    attributes: Mapping[str, Any],
    *,
    observation_type: Optional[str] = None,
    observation_name: Optional[str] = None,
    allow_legacy_type_name: bool = True,
) -> str:
    """
    由 attributes 推导聚合桶；优先 agent.step.id / agent.tool.name。

    allow_legacy_type_name=False 时禁止 type:name 回退（P2+ Builder 默认）。
    """
    step_id = attributes.get("agent.step.id")
    if step_id is not None and str(step_id).strip():
        return f"step:{step_id}"
    tool = attributes.get("agent.tool.name")
    if tool is not None and str(tool).strip():
        retry = attributes.get("agent.is_retry")
        suffix = ":retry" if retry in (True, "true", "True", 1) else ""
        return f"tool:{tool}{suffix}"
    kind = attributes.get("agent.step.kind")
    if kind is not None and str(kind).strip():
        return f"kind:{kind}"

    # agent.member.name 作次级聚合键（Member 协作 span 无 step.id 时的 fallback）
    member = attributes.get("agent.member.name")
    if member is not None and str(member).strip():
        role = attributes.get("agent.role") or "unknown"
        return f"member:{member}:{role}"

    if allow_legacy_type_name and observation_type and observation_name:
        return f"legacy:{observation_type}:{observation_name}"
    return "legacy:unknown"


def _coerce_value(v: Any) -> Any:
    if isinstance(v, (str, int, bool)):
        return v
    if isinstance(v, float):
        return v
    return str(v)
