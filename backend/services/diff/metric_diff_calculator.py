"""metric_diff 兼容别名：合并 outcome + cost（P3-B5）。"""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy.ext.asyncio import AsyncSession

from services.diff.base import DiffCalculator
from services.diff.cost_diff_calculator import CostDiffCalculator
from services.diff.diff_types import METRIC_DIFF, SCHEMA_VERSION
from services.diff.outcome_diff_calculator import OutcomeDiffCalculator


class MetricDiffCalculator(DiffCalculator):
    diff_type = METRIC_DIFF
    schema_version = SCHEMA_VERSION

    async def compute(
        self,
        session: AsyncSession,
        *,
        left_run_id: str,
        right_run_id: str,
    ) -> Dict[str, Any]:
        outcome = await OutcomeDiffCalculator().compute(
            session, left_run_id=left_run_id, right_run_id=right_run_id
        )
        cost = await CostDiffCalculator().compute(
            session, left_run_id=left_run_id, right_run_id=right_run_id
        )
        return {
            "metrics": outcome.get("metrics") or {},
            "run_level": cost.get("run_level") or {},
            "alias_of": ["outcome_diff", "cost_diff"],
        }
