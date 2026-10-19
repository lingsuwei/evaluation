"""场景模板步骤工具：启用步过滤、列定义（配置页与打分共用）。"""
from __future__ import annotations

from typing import Any, Dict, List


def filter_enabled_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [s for s in (steps or []) if s.get("enabled", True)]


def template_with_active_steps_only(tpl: Dict[str, Any]) -> Dict[str, Any]:
    if not tpl:
        return tpl
    out = dict(tpl)
    out["steps"] = filter_enabled_steps(tpl.get("steps") or [])
    return out


def active_step_ids(tpl: Dict[str, Any]) -> List[str]:
    return [str(s["id"]) for s in filter_enabled_steps(tpl.get("steps") or []) if s.get("id")]


def step_columns_from_template(tpl: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "step_key": s["step_key"],
            "name": s["name"],
            "weight": s["weight"],
            "required": s["required"],
            "enabled": s.get("enabled", True),
        }
        for s in filter_enabled_steps(tpl.get("steps") or [])
    ]


def merge_step_columns_union(
    templates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """多题模板步骤列并集（保持 step_order）。"""
    seen: Dict[str, Dict[str, Any]] = {}
    ordered: List[Dict[str, Any]] = []
    for tpl in templates:
        for s in filter_enabled_steps(tpl.get("steps") or []):
            sk = str(s.get("step_key") or "")
            if not sk or sk in seen:
                continue
            col = {
                "step_key": sk,
                "name": s.get("name") or sk,
                "weight": s.get("weight"),
                "required": s.get("required", True),
                "enabled": True,
            }
            seen[sk] = col
            ordered.append(col)
    return ordered
