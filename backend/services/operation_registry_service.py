"""
Operation Registry CRUD 服务（A2a / §7.5.4）。

提供 AgentAssetSnapshot 和 TraceOperationRule 的基础增删改查。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentAssetSnapshot, TraceOperationRule
from logger import log


# ── AgentAssetSnapshot ──────────────────────────────────────────


async def upsert_asset_snapshot(
    session: AsyncSession,
    *,
    logical_agent_id: str,
    asset_type: str,
    asset_hash: str,
    agent_version: Optional[str] = None,
    asset_source: Optional[str] = None,
    asset_payload: Optional[Dict[str, Any]] = None,
) -> AgentAssetSnapshot:
    """幂等写入资产快照；同一 agent + type + hash 已存在时更新 payload。"""
    stmt = select(AgentAssetSnapshot).where(
        AgentAssetSnapshot.logical_agent_id == logical_agent_id,
        AgentAssetSnapshot.asset_type == asset_type,
        AgentAssetSnapshot.asset_hash == asset_hash,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        existing.agent_version = agent_version or existing.agent_version
        existing.asset_source = asset_source or existing.asset_source
        if asset_payload is not None:
            existing.asset_payload = asset_payload
        existing.updated_at = datetime.utcnow()
        await session.flush()
        return existing

    snapshot = AgentAssetSnapshot(
        id=f"asset_{uuid.uuid4().hex[:12]}",
        logical_agent_id=logical_agent_id,
        agent_version=agent_version,
        asset_type=asset_type,
        asset_hash=asset_hash,
        asset_source=asset_source,
        asset_payload=asset_payload,
        compile_status="pending",
        registry_version=1,
    )
    session.add(snapshot)
    await session.flush()
    log(
        "INFO",
        "REGISTRY",
        "资产快照写入",
        logical_agent_id=logical_agent_id,
        asset_type=asset_type,
        asset_hash=asset_hash[:12],
    )
    return snapshot


async def get_asset_snapshots_for_agent(
    session: AsyncSession,
    logical_agent_id: str,
) -> Sequence[AgentAssetSnapshot]:
    stmt = (
        select(AgentAssetSnapshot)
        .where(AgentAssetSnapshot.logical_agent_id == logical_agent_id)
        .order_by(AgentAssetSnapshot.asset_type, AgentAssetSnapshot.created_at.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


# ── TraceOperationRule ──────────────────────────────────────────


async def upsert_operation_rule(
    session: AsyncSession,
    *,
    logical_agent_id: str,
    operation_id: str,
    match_rules: Dict[str, Any],
    display_name: Optional[str] = None,
    source_asset_id: Optional[str] = None,
    priority: int = 0,
    confidence: float = 1.0,
    binding_reason_template: Optional[str] = None,
    registry_version: int = 1,
) -> TraceOperationRule:
    """幂等写入操作规则；同一 agent + operation_id + version 已存在时更新。"""
    stmt = select(TraceOperationRule).where(
        TraceOperationRule.logical_agent_id == logical_agent_id,
        TraceOperationRule.operation_id == operation_id,
        TraceOperationRule.registry_version == registry_version,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        existing.match_rules = match_rules
        existing.display_name = display_name or existing.display_name
        existing.source_asset_id = source_asset_id or existing.source_asset_id
        existing.priority = priority
        existing.confidence = confidence
        existing.binding_reason_template = binding_reason_template or existing.binding_reason_template
        existing.enabled = True
        existing.updated_at = datetime.utcnow()
        await session.flush()
        return existing

    rule = TraceOperationRule(
        id=f"tor_{uuid.uuid4().hex[:8]}",
        logical_agent_id=logical_agent_id,
        operation_id=operation_id,
        display_name=display_name,
        source_asset_id=source_asset_id,
        match_rules=match_rules,
        priority=priority,
        confidence=confidence,
        binding_reason_template=binding_reason_template,
        registry_version=registry_version,
    )
    session.add(rule)
    await session.flush()
    log(
        "INFO",
        "REGISTRY",
        "操作规则写入",
        logical_agent_id=logical_agent_id,
        operation_id=operation_id,
    )
    return rule


async def get_active_rules_for_agent(
    session: AsyncSession,
    logical_agent_id: str,
    *,
    registry_version: Optional[int] = None,
) -> Sequence[TraceOperationRule]:
    """获取 Agent 当前生效的操作规则，按 priority 降序。"""
    stmt = (
        select(TraceOperationRule)
        .where(
            TraceOperationRule.logical_agent_id == logical_agent_id,
            TraceOperationRule.enabled.is_(True),
        )
        .order_by(TraceOperationRule.priority.desc(), TraceOperationRule.operation_id)
    )
    if registry_version is not None:
        stmt = stmt.where(TraceOperationRule.registry_version == registry_version)
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_latest_registry_version(
    session: AsyncSession,
    logical_agent_id: str,
) -> int:
    """获取 Agent 当前最大 registry_version，无规则时返回 0。"""
    stmt = select(TraceOperationRule.registry_version).where(
        TraceOperationRule.logical_agent_id == logical_agent_id,
    )
    result = await session.execute(stmt)
    versions = [r[0] for r in result.fetchall()]
    return max(versions) if versions else 0


async def deactivate_old_rules(
    session: AsyncSession,
    logical_agent_id: str,
    *,
    keep_version: int,
) -> int:
    """禁用旧版本规则（enabled=False），返回受影响行数。"""
    stmt = (
        update(TraceOperationRule)
        .where(
            TraceOperationRule.logical_agent_id == logical_agent_id,
            TraceOperationRule.registry_version < keep_version,
            TraceOperationRule.enabled.is_(True),
        )
        .values(enabled=False, updated_at=datetime.utcnow())
    )
    result = await session.execute(stmt)
    await session.flush()
    count = result.rowcount  # type: ignore[attr-defined]
    if count:
        log(
            "INFO",
            "REGISTRY",
            "旧版本规则已禁用",
            logical_agent_id=logical_agent_id,
            keep_version=keep_version,
            deactivated=count,
        )
    return count


async def delete_rules_for_agent(
    session: AsyncSession,
    logical_agent_id: str,
) -> int:
    """删除 Agent 所有规则（用于全量重建）。"""
    stmt = delete(TraceOperationRule).where(
        TraceOperationRule.logical_agent_id == logical_agent_id,
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.rowcount  # type: ignore[attr-defined]
