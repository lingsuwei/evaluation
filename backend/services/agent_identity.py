"""
Agent 业务名称与 logical_agent_id 的解析与唯一性约束。
名称与逻辑 Agent 强绑定；过程点 / Trace 探索界面使用名称，内部仍用 logical_agent_id。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Agent


async def build_agent_identity_maps(session: AsyncSession) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    返回 (logical_agent_id -> 展示名称, 名称 -> logical_agent_id)。
    同一 logical_agent_id 多版本取最近创建记录的名称。
    """
    result = await session.execute(
        select(Agent)
        .where(Agent.logical_agent_id.isnot(None))
        .order_by(Agent.logical_agent_id, Agent.created_at.desc())
    )
    lid_to_name: Dict[str, str] = {}
    name_to_lid: Dict[str, str] = {}
    for row in result.scalars().all():
        lid = (row.logical_agent_id or "").strip()
        name = (row.name or "").strip()
        if not lid or not name:
            continue
        if lid not in lid_to_name:
            lid_to_name[lid] = name
        if name not in name_to_lid:
            name_to_lid[name] = lid
    return lid_to_name, name_to_lid


async def list_distinct_agent_names(session: AsyncSession) -> List[str]:
    """每个逻辑 Agent 一条展示名称，供下拉筛选。"""
    lid_to_name, _ = await build_agent_identity_maps(session)
    return sorted(lid_to_name.values())


async def resolve_logical_agent_id_by_name(session: AsyncSession, agent_name: str) -> str:
    """将业务名称解析为 logical_agent_id；名称须能唯一对应一个逻辑 Agent。"""
    name = (agent_name or "").strip()
    if not name:
        raise HTTPException(400, "请填写 Agent 名称")
    _, name_to_lid = await build_agent_identity_maps(session)
    lid = name_to_lid.get(name)
    if not lid:
        raise HTTPException(400, f"未找到名称为「{name}」的 Agent，请先在 Agent 管理中注册")
    # 反向校验：无其它 logical_agent_id 占用同一名称
    lid_to_name, _ = await build_agent_identity_maps(session)
    conflicting = [l for l, n in lid_to_name.items() if n == name and l != lid]
    if conflicting:
        raise HTTPException(
            400,
            f"Agent 名称「{name}」对应多个逻辑 Agent，请先在 Agent 管理中修正名称唯一性",
        )
    return lid


def default_logical_agent_id(agent: Agent) -> str:
    """旧数据无 logical_agent_id 时生成稳定族 ID。"""
    existing = (agent.logical_agent_id or "").strip()
    if existing:
        return existing
    return f"la_{agent.id}"


async def resolve_logical_agent_id_for_register(
    session: AsyncSession,
    *,
    logical_agent_id: Optional[str],
    parent_agent_id: Optional[str],
) -> tuple[str, Optional[Agent]]:
    """
    解析注册用的 logical_agent_id。
    新版本：继承 parent；parent 无逻辑 ID 时补写并返回 parent 供调用方 flush。
    """
    lid = (logical_agent_id or "").strip()
    parent: Optional[Agent] = None
    if parent_agent_id:
        parent = await session.get(Agent, parent_agent_id)
        if not parent:
            raise HTTPException(404, f"源 Agent {parent_agent_id} 不存在，无法注册新版本")
        if not lid:
            lid = default_logical_agent_id(parent)
        if not (parent.logical_agent_id or "").strip():
            parent.logical_agent_id = lid
    return lid, parent


async def ensure_agent_name_unique_for_logical(
    session: AsyncSession,
    name: str,
    logical_agent_id: Optional[str],
    *,
    exclude_agent_id: Optional[str] = None,
    parent_agent_id: Optional[str] = None,
) -> None:
    """名称不得被其它 logical_agent_id 占用（同族多版本共享名称时跳过）。"""
    name = (name or "").strip()
    lid = (logical_agent_id or "").strip()
    if not name:
        return
    result = await session.execute(select(Agent).where(Agent.name == name))
    for row in result.scalars().all():
        if exclude_agent_id and row.id == exclude_agent_id:
            continue
        if parent_agent_id and row.id == parent_agent_id:
            continue
        other_lid = (row.logical_agent_id or "").strip()
        if lid and other_lid == lid:
            continue
        raise HTTPException(
            400,
            f"Agent 名称「{name}」已被其它逻辑 Agent 使用；"
            f"同逻辑 Agent 多版本应共享名称，请使用列表「新版本」注册",
        )


def agent_display_name_from_trace(
    trace: dict,
    *,
    lid_to_name: Optional[Dict[str, str]] = None,
) -> str:
    """从 trace 元数据得到界面展示用 Agent 名称（不暴露 logical_agent_id）。"""
    meta = trace.get("metadata") or {}
    name = meta.get("agent_name")
    if name is not None and str(name).strip():
        return str(name).strip()
    lid = meta.get("logical_agent_id") or meta.get("agent_id")
    if lid is not None:
        lid_s = str(lid).strip()
        if lid_s and lid_to_name and lid_s in lid_to_name:
            return lid_to_name[lid_s]
    tags = trace.get("tags") or []
    for tag in tags:
        if isinstance(tag, str) and tag.startswith("logical_agent:"):
            lid_s = tag[len("logical_agent:") :].strip()
            if lid_s and lid_to_name and lid_s in lid_to_name:
                return lid_to_name[lid_s]
    return ""


def trace_matches_agent_name(
    trace: dict,
    agent_name: str,
    *,
    name_to_lid: Optional[Dict[str, str]] = None,
) -> bool:
    """按业务名称筛选 trace（兼容仅含 logical_agent_id 的旧 trace）。"""
    target = (agent_name or "").strip()
    if not target:
        return True
    meta = trace.get("metadata") or {}
    meta_name = meta.get("agent_name")
    if meta_name is not None and str(meta_name).strip() == target:
        return True
    if name_to_lid:
        expected_lid = name_to_lid.get(target)
        if expected_lid:
            lid = meta.get("logical_agent_id") or meta.get("agent_id")
            if lid is not None and str(lid).strip() == expected_lid:
                return True
            tags = trace.get("tags") or []
            if f"logical_agent:{expected_lid}" in tags:
                return True
    return False
