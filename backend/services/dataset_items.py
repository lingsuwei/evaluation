"""
数据集条目稳定标识（C-01 / ADR-030）。

上传与评测加载时为每项保证 dataset_item_id（UUID），并可选生成 question_key（内容哈希）。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple


def derive_question_key(item: Mapping[str, Any]) -> str:
    """跨数据集对齐用的内容指纹（非全局业务主键）。"""
    question = (
        item.get("question")
        or item.get("input")
        or item.get("query")
        or item.get("prompt")
        or ""
    )
    ground_truth = item.get("ground_truth") or item.get("expected_output") or ""
    payload = json.dumps(
        {"q": str(question).strip(), "gt": str(ground_truth).strip()},
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"qk_{digest}"


def ensure_dataset_item_id(item: Dict[str, Any]) -> str:
    """返回稳定 id；若条目无 id 则生成并写回 item['id']。"""
    existing = item.get("id") or item.get("dataset_item_id")
    if existing and str(existing).strip():
        stable = str(existing).strip()
        item["id"] = stable
        return stable
    stable = f"ditem_{uuid.uuid4().hex[:12]}"
    item["id"] = stable
    return stable


def normalize_dataset_items(items: List[Any]) -> List[Dict[str, Any]]:
    """
    规范化数据集 JSON 数组：每项为 dict，含稳定 id 与 question_key。
    上传与评测加载均应调用，避免仅依赖数组下标。
    """
    out: List[Dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        ensure_dataset_item_id(item)
        if not item.get("question_key"):
            item["question_key"] = derive_question_key(item)
        sk = item.get("scenario_key") or item.get("scenario")
        if sk is not None:
            item["scenario_key"] = str(sk).strip() or None
        out.append(item)
    return out


def get_item_stable_ids(item: Mapping[str, Any], *, index: int) -> Tuple[str, str, int]:
    """(dataset_item_id, question_key, question_index)。"""
    ditem = str(item.get("id") or item.get("dataset_item_id") or "").strip()
    if not ditem:
        ditem = f"ditem_idx_{index}"
    qk = str(item.get("question_key") or derive_question_key(item))
    return ditem, qk, index
