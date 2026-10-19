"""checkpoint_diff：Behavioral 过程点对比（P2 数据 / P3 注册）。"""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import CheckpointResult
from services.diff.base import DiffCalculator
from services.diff.diff_types import CHECKPOINT_DIFF, SCHEMA_VERSION


class CheckpointDiffCalculator(DiffCalculator):
    diff_type = CHECKPOINT_DIFF
    schema_version = SCHEMA_VERSION

    async def compute(
        self,
        session: AsyncSession,
        *,
        left_run_id: str,
        right_run_id: str,
    ) -> Dict[str, Any]:
        left = await self._load(session, left_run_id)
        right = await self._load(session, right_run_id)
        checkpoints: Dict[str, Any] = {}
        for cid in sorted(set(left) | set(right)):
            l_pass = left.get(cid)
            r_pass = right.get(cid)
            checkpoints[cid] = {
                "passed_left": l_pass,
                "passed_right": r_pass,
                "same": l_pass == r_pass if l_pass is not None and r_pass is not None else None,
            }
        return {"checkpoints": checkpoints}

    async def _load(self, session: AsyncSession, run_id: str) -> Dict[str, bool]:
        rows = (
            await session.execute(
                select(CheckpointResult).where(CheckpointResult.agent_run_id == run_id)
            )
        ).scalars().all()
        return {r.checkpoint_id: bool(r.passed) for r in rows}
