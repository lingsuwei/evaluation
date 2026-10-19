"""评测四维结论阈值：系统默认 + 任务级覆盖。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, List, Optional

from database import EvaluationTask

from services.dimension_verdict_constants import (
    PERF_PASS_RATE_ELEVATED,
    PERF_PASS_RATE_WITHIN,
    PROCESS_CHAIN_PASS_WARN,
    QUALITY_PASS_RATE_PASS,
    QUALITY_PASS_RATE_WARN,
    QUALITY_TOTAL_PASS,
    QUALITY_TOTAL_WARN,
    SCENARIO_PROCESS_PASS,
    SCENARIO_PROCESS_WARN,
    SCENARIO_REQUIRED_PASS,
    SCENARIO_REQUIRED_WARN,
    SCENARIO_SCORE_STD_VOLATILE,
    STRUCT_DRIFT_RATIO_SIGNIFICANT,
)


@dataclass(frozen=True)
class VerdictThresholds:
    quality_pass_rate_pass: float = QUALITY_PASS_RATE_PASS
    quality_pass_rate_warn: float = QUALITY_PASS_RATE_WARN
    quality_total_pass: float = QUALITY_TOTAL_PASS
    quality_total_warn: float = QUALITY_TOTAL_WARN
    process_chain_pass_warn: float = PROCESS_CHAIN_PASS_WARN
    scenario_process_pass: float = SCENARIO_PROCESS_PASS
    scenario_process_warn: float = SCENARIO_PROCESS_WARN
    scenario_required_pass: float = SCENARIO_REQUIRED_PASS
    scenario_required_warn: float = SCENARIO_REQUIRED_WARN
    scenario_score_std_volatile: float = SCENARIO_SCORE_STD_VOLATILE
    struct_drift_ratio_significant: float = STRUCT_DRIFT_RATIO_SIGNIFICANT
    perf_pass_rate_within: float = PERF_PASS_RATE_WITHIN
    perf_pass_rate_elevated: float = PERF_PASS_RATE_ELEVATED

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)

    @classmethod
    def defaults(cls) -> VerdictThresholds:
        return cls()

    @classmethod
    def from_mapping(cls, raw: Optional[Dict[str, Any]]) -> VerdictThresholds:
        base = cls.defaults().to_dict()
        if not raw:
            return cls(**base)
        allowed = {f.name for f in fields(cls)}
        for key, val in raw.items():
            if key not in allowed or val is None:
                continue
            try:
                base[key] = float(val)
            except (TypeError, ValueError):
                continue
        return cls(**base)


def _thresholds_from_task_json(task: Optional[EvaluationTask]) -> Optional[Dict[str, Any]]:
    if task is None:
        return None
    raw = getattr(task, "verdict_thresholds", None)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def resolve_verdict_thresholds(task: Optional[EvaluationTask]) -> VerdictThresholds:
    custom = _thresholds_from_task_json(task)
    return VerdictThresholds.from_mapping(custom)


def custom_threshold_keys(task: Optional[EvaluationTask]) -> List[str]:
    custom = _thresholds_from_task_json(task)
    if not custom:
        return []
    allowed = {f.name for f in fields(VerdictThresholds)}
    return sorted(k for k in custom.keys() if k in allowed)


def threshold_meta(task: Optional[EvaluationTask]) -> Dict[str, Any]:
    custom_keys = custom_threshold_keys(task)
    effective = resolve_verdict_thresholds(task)
    return {
        "effective": effective.to_dict(),
        "defaults": VerdictThresholds.defaults().to_dict(),
        "source": "task_custom" if custom_keys else "system_default",
        "custom_keys": custom_keys,
    }


def validate_threshold_patch(patch: Dict[str, Any]) -> Dict[str, float]:
    """校验并归一化 PATCH 字段；非法键忽略，非法值抛 ValueError。"""
    allowed = {f.name for f in fields(VerdictThresholds)}
    out: Dict[str, float] = {}
    for key, val in (patch or {}).items():
        if key not in allowed:
            continue
        if val is None:
            continue
        try:
            num = float(val)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"阈值 {key} 必须为数字") from exc
        _validate_range(key, num)
        out[key] = num
    return out


def _validate_range(key: str, val: float) -> None:
    if key.endswith("_pass_rate_pass") or key.endswith("_pass_rate_warn"):
        if key.startswith("perf_"):
            if not 0 <= val <= 100:
                raise ValueError(f"{key} 须在 0–100 之间")
        elif not 0 <= val <= 1:
            raise ValueError(f"{key} 须在 0–1 之间")
    elif key.endswith("_pass") or key.endswith("_warn") or key.startswith("quality_total"):
        if not 0 <= val <= 1:
            raise ValueError(f"{key} 须在 0–1 之间")
    elif key == "struct_drift_ratio_significant":
        if not 0 < val <= 1:
            raise ValueError("struct_drift_ratio_significant 须在 (0, 1] 之间")
    elif key == "scenario_score_std_volatile":
        if not 0 <= val <= 1:
            raise ValueError("scenario_score_std_volatile 须在 0–1 之间")


def merge_task_threshold_patch(
    task: Optional[EvaluationTask], patch: Dict[str, float]
) -> Dict[str, float]:
    current = _thresholds_from_task_json(task) or {}
    merged = {**current, **patch}
    return merged


def serialize_thresholds_for_storage(thresholds: Dict[str, float]) -> str:
    return json.dumps(thresholds, ensure_ascii=False)
