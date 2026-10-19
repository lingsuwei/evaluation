"""
过程点 match_rules 校验与观测匹配（M2 rule 模式）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from services.otel_attributes import build_match_document


class MatchRulesValidationError(ValueError):
    pass


def validate_match_rules(raw: Any) -> Dict[str, Any]:
    """校验并规范化 match_rules JSON。"""
    if not isinstance(raw, dict):
        raise MatchRulesValidationError("match_rules 必须为 JSON 对象")
    mode = str(raw.get("mode") or "any").strip().lower()
    if mode not in ("any", "all"):
        raise MatchRulesValidationError("mode 必须为 any 或 all")
    rules = raw.get("rules")
    if not isinstance(rules, list) or not rules:
        raise MatchRulesValidationError("rules 必须为非空数组")
    normalized: List[Dict[str, Any]] = []
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise MatchRulesValidationError(f"rules[{i}] 必须为对象")
        rtype = str(rule.get("type") or "").strip()
        op = str(rule.get("op") or "").strip()
        if rtype not in ("observation_name", "observation_type", "metadata", "jsonpath"):
            raise MatchRulesValidationError(f"rules[{i}].type 不支持: {rtype}")
        if rtype == "metadata":
            key = str(rule.get("key") or "").strip()
            if not key:
                raise MatchRulesValidationError(f"rules[{i}].metadata 缺少 key")
        if rtype == "jsonpath":
            path = str(rule.get("path") or "").strip()
            if not path.startswith("$."):
                raise MatchRulesValidationError(
                    f"rules[{i}].jsonpath.path 必须以 $. 开头，如 $.attributes.agent.step.id"
                )
        allowed_ops = {
            "observation_name": {"eq", "contains", "regex"},
            "observation_type": {"eq"},
            "metadata": {"eq", "contains", "in"},
            "jsonpath": {"eq", "contains", "in", "regex"},
        }
        if op not in allowed_ops[rtype]:
            raise MatchRulesValidationError(f"rules[{i}].op 对 {rtype} 无效: {op}")
        if "value" not in rule:
            raise MatchRulesValidationError(f"rules[{i}] 缺少 value")
        normalized.append(dict(rule))
    return {"mode": mode, "rules": normalized}


def _obs_meta_dict(obs: Dict[str, Any]) -> Dict[str, Any]:
    meta = obs.get("metadata")
    return meta if isinstance(meta, dict) else {}


def _jsonpath_get(document: Dict[str, Any], path: str) -> Any:
    """轻量 JSONPath：仅支持 $.a.b.c 点分路径（无 filter / 数组下标）。"""
    if not path.startswith("$."):
        return None
    cur: Any = document
    for part in path[2:].split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _compare_value(actual: Any, op: str, value: Any) -> bool:
    if op == "eq":
        if isinstance(value, bool):
            return actual is value or (
                isinstance(actual, str) and actual.lower() == str(value).lower()
            )
        return str(actual) == str(value)
    if op == "contains":
        return str(value or "").lower() in str(actual or "").lower()
    if op == "in":
        if not isinstance(value, list):
            return False
        return str(actual) in [str(v) for v in value]
    if op == "regex":
        try:
            return bool(re.search(str(value), str(actual or "")))
        except re.error:
            return False
    return False


def _rule_matches(obs: Dict[str, Any], rule: Dict[str, Any]) -> bool:
    rtype = rule["type"]
    op = rule["op"]
    value = rule.get("value")

    if rtype == "observation_name":
        name = str(obs.get("name") or "")
        target = str(value or "")
        if op == "eq":
            return name == target
        if op == "contains":
            return target.lower() in name.lower()
        if op == "regex":
            try:
                return bool(re.search(str(value), name))
            except re.error:
                return False

    if rtype == "observation_type":
        otype = str(obs.get("type") or "")
        return otype == str(value or "")

    if rtype == "metadata":
        key = str(rule.get("key") or "")
        actual = _obs_meta_dict(obs).get(key)
        return _compare_value(actual, op, value)

    if rtype == "jsonpath":
        path = str(rule.get("path") or "")
        doc = build_match_document(obs)
        actual = _jsonpath_get(doc, path)
        return _compare_value(actual, op, value)

    return False


def find_matching_observations(
    observations: List[Dict[str, Any]],
    match_rules: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """返回命中规则的过程点对应 observation 列表。"""
    mode = match_rules.get("mode", "any")
    rules = match_rules.get("rules") or []
    hits: List[Dict[str, Any]] = []
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        results = [_rule_matches(obs, r) for r in rules]
        ok = all(results) if mode == "all" else any(results)
        if ok:
            hits.append(obs)
    return hits


def evaluate_checkpoint_on_trace(
    *,
    checkpoint_name: str,
    order: Optional[int],
    required: bool,
    match_rules: Dict[str, Any],
    observations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """单过程点匹配结果（preview / M3 引擎共用）。"""
    matched = find_matching_observations(observations, match_rules)
    first = matched[0] if matched else None
    hit = len(matched) > 0
    order_ok: Optional[bool] = None
    if order is not None and hit:
        # 简化：按 observation startTime 排序后，首个命中在序列中的位置
        sorted_obs = sorted(
            observations,
            key=lambda o: str(o.get("startTime") or o.get("endTime") or ""),
        )
        try:
            idx = next(i for i, o in enumerate(sorted_obs) if o.get("id") == first.get("id"))
            order_ok = idx + 1 == order
        except (StopIteration, AttributeError):
            order_ok = None

    return {
        "checkpoint_name": checkpoint_name,
        "hit": hit,
        "required": required,
        "order": order,
        "order_ok": order_ok,
        "matched_count": len(matched),
        "matched_observation_id": first.get("id") if first else None,
        "matched_observation_name": first.get("name") if first else None,
        "evidence": {
            "matched_ids": [o.get("id") for o in matched if o.get("id")],
        },
    }
