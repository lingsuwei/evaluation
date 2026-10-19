"""outcome_diff：读 evaluation_results（P3 §10.3.1）。"""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import EvaluationResult
from services.diff.base import DiffCalculator
from services.diff.diff_types import OUTCOME_DIFF, SCHEMA_VERSION


class OutcomeDiffCalculator(DiffCalculator):
    diff_type = OUTCOME_DIFF
    schema_version = SCHEMA_VERSION

    async def compute(
        self,
        session: AsyncSession,
        *,
        left_run_id: str,
        right_run_id: str,
    ) -> Dict[str, Any]:
        left_rows = await self._load_results(session, left_run_id)
        right_rows = await self._load_results(session, right_run_id)
        metrics: Dict[str, Any] = {}
        keys = sorted(set(left_rows) | set(right_rows))
        for key in keys:
            l = left_rows.get(key)
            r = right_rows.get(key)
            left_score = l["score"] if l else None
            right_score = r["score"] if r else None
            delta = None
            if left_score is not None and right_score is not None:
                delta = round(right_score - left_score, 6)
            metrics[key] = {
                "left": left_score,
                "right": right_score,
                "delta": delta,
                "passed_left": l["passed"] if l else None,
                "passed_right": r["passed"] if r else None,
            }
        return {"metrics": metrics}

    async def _load_results(
        self, session: AsyncSession, agent_run_id: str
    ) -> Dict[str, Dict[str, Any]]:
        rows = (
            await session.execute(
                select(EvaluationResult).where(
                    EvaluationResult.agent_run_id == agent_run_id
                )
            )
        ).scalars().all()
        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            score = row.avg_score if row.avg_score is not None else row.score
            out[row.metric_name] = {
                "score": float(score) if score is not None else None,
                "passed": bool(row.passed) if row.passed is not None else None,
            }
        return out
