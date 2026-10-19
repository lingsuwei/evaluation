"""
评测 Context（eval_context）读写 — ADR-028。

AgentRun 表示一次纯粹推理；task_id / question_id 等仅存在于 eval_context（评测）或为空（生产）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from database import AgentRun


def build_eval_context(
    *,
    task_id: str,
    dataset_id: str,
    question_id: int,
    run_index: int = 0,
    dataset_item_id: Optional[str] = None,
    question_key: Optional[str] = None,
    scenario_key: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {
        "task_id": task_id,
        "dataset_id": dataset_id,
        "question_id": question_id,
        "run_index": run_index,
    }
    if dataset_item_id:
        ctx["dataset_item_id"] = dataset_item_id
    if question_key:
        ctx["question_key"] = question_key
    if scenario_key:
        ctx["scenario_key"] = scenario_key
    if extra:
        ctx.update(extra)
    return ctx


def parse_eval_context(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def get_eval_field(run: AgentRun, key: str, default: Any = None) -> Any:
    ctx = parse_eval_context(run.eval_context)
    if key in ctx and ctx[key] is not None:
        return ctx[key]
    # 过渡期：镜像列
    legacy = {
        "task_id": run.task_id,
        "dataset_id": run.dataset_id,
        "question_id": run.question_id,
        "run_index": run.run_index,
    }
    val = legacy.get(key, default)
    return val if val is not None else default


def get_task_id(run: AgentRun) -> Optional[str]:
    v = get_eval_field(run, "task_id")
    return str(v) if v else None


def get_dataset_id(run: AgentRun) -> Optional[str]:
    v = get_eval_field(run, "dataset_id")
    return str(v) if v else None


def get_dataset_item_id(run: AgentRun) -> Optional[str]:
    v = get_eval_field(run, "dataset_item_id")
    return str(v).strip() if v else None


def get_question_key(run: AgentRun) -> Optional[str]:
    v = get_eval_field(run, "question_key")
    return str(v).strip() if v else None


def get_question_id(run: AgentRun) -> Optional[int]:
    v = get_eval_field(run, "question_id")
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def sync_legacy_columns_from_context(run: AgentRun) -> None:
    """双写镜像列，供 SQLite 旧索引与未迁移查询使用。"""
    ctx = parse_eval_context(run.eval_context)
    if not ctx:
        return
    if ctx.get("task_id"):
        run.task_id = str(ctx["task_id"])
    if ctx.get("dataset_id"):
        run.dataset_id = str(ctx["dataset_id"])
    if ctx.get("question_id") is not None:
        run.question_id = int(ctx["question_id"])
    if ctx.get("run_index") is not None:
        run.run_index = int(ctx["run_index"])
