"""Diff Calculator 抽象基类。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict

from sqlalchemy.ext.asyncio import AsyncSession


class DiffCalculator(ABC):
    diff_type: str
    schema_version: str = "1.0"

    @abstractmethod
    async def compute(
        self,
        session: AsyncSession,
        *,
        left_run_id: str,
        right_run_id: str,
    ) -> Dict[str, Any]:
        """返回 diff payload（不含 diff_type / run ids，由 service 包装）。"""
