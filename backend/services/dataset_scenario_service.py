"""数据集条目 ↔ 场景模板解析（ST-2-1）。"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Dataset
from services.dataset_items import normalize_dataset_items


def load_dataset_items(raw_data: str | None) -> List[Dict[str, Any]]:
    if not raw_data:
        return []
    try:
        parsed = json.loads(raw_data)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return normalize_dataset_items(parsed)


async def get_dataset_default_scenario_key(
    session: AsyncSession, dataset_id: Optional[str]
) -> Optional[str]:
    if not dataset_id:
        return None
    ds = await session.get(Dataset, dataset_id)
    if not ds:
        return None
    key = (getattr(ds, "default_scenario_key", None) or "").strip()
    return key or None


def scenario_key_from_item(item: Dict[str, Any]) -> Optional[str]:
    raw = item.get("scenario_key") or item.get("scenario")
    if raw is None:
        return None
    key = str(raw).strip()
    return key or None


async def get_item_scenario_key(
    session: AsyncSession,
    dataset_id: Optional[str],
    question_id: Optional[int],
) -> Tuple[Optional[str], Optional[str]]:
    """
    返回 (item_scenario_key, dataset_default_scenario_key)。
    question_id 为评测集数组下标。
    """
    if dataset_id is None or question_id is None:
        return None, await get_dataset_default_scenario_key(session, dataset_id)
    ds = await session.get(Dataset, dataset_id)
    if not ds:
        return None, None
    default_key = await get_dataset_default_scenario_key(session, dataset_id)
    items = load_dataset_items(ds.data)
    if question_id < 0 or question_id >= len(items):
        return None, default_key
    return scenario_key_from_item(items[question_id]), default_key
