"""onto-eval AI 对话式配置 — 会话 CRUD。

阶段 1：会话生命周期管理 + 草稿落库。所有函数 async，走 async_session。
阶段 2：ChatMessage 扩展 tool_* 字段，由 orchestrator 追加。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from sqlalchemy import select, delete, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from database import ChatSession, ChatMessage, async_session


def _gen_session_id() -> str:
    return f"chat_{uuid.uuid4().hex}"


def _gen_title(intent_text: str) -> str:
    """从意图文本截取前 30 字作为标题。"""
    if not intent_text:
        return "新会话"
    text = intent_text.strip().replace("\n", " ")
    return text[:30] + ("…" if len(text) > 30 else "")


async def create_session(
    intent_text: str,
    model_key: Optional[str] = None,
    session: Optional[AsyncSession] = None,
) -> ChatSession:
    """新建会话，状态 draft。"""
    own_session = session is None
    if own_session:
        async with async_session() as session:
            return await create_session(intent_text, model_key, session)

    obj = ChatSession(
        id=_gen_session_id(),
        title=_gen_title(intent_text),
        status="draft",
        intent=None,
        model_key=model_key,
        intent_text=intent_text,
        draft_request=None,
        confidence=None,
        missing_fields=None,
        error_message=None,
    )
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


async def get_session(
    session_id: str,
    session: Optional[AsyncSession] = None,
) -> Optional[ChatSession]:
    own_session = session is None
    if own_session:
        async with async_session() as session:
            return await get_session(session_id, session)

    result = await session.execute(
        select(ChatSession).where(ChatSession.id == session_id)
    )
    return result.scalar_one_or_none()


async def set_intent_text(
    session_id: str,
    intent_text: str,
    session: Optional[AsyncSession] = None,
) -> None:
    """更新会话 intent_text + title（阶段 2 首条消息回填）。"""
    own_session = session is None
    if own_session:
        async with async_session() as session:
            await set_intent_text(session_id, intent_text, session)
            return

    obj = await get_session(session_id, session)
    if not obj:
        return
    obj.intent_text = intent_text
    if not obj.title or obj.title == "新会话":
        obj.title = _gen_title(intent_text)
    await session.commit()


async def list_sessions(
    status: Optional[str] = None,
    intent: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    session: Optional[AsyncSession] = None,
) -> tuple[list[ChatSession], int]:
    """分页列表，按 created_at 倒序。返回 (列表, 总数)。"""
    own_session = session is None
    if own_session:
        async with async_session() as session:
            return await list_sessions(status, intent, page, page_size, session)

    stmt = select(ChatSession)
    if status:
        stmt = stmt.where(ChatSession.status == status)
    if intent:
        stmt = stmt.where(ChatSession.intent == intent)
    stmt = stmt.order_by(ChatSession.created_at.desc())

    # 总数
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar() or 0

    # 分页
    offset = (page - 1) * page_size
    stmt = stmt.offset(offset).limit(page_size)
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows), total


async def update_draft(
    session_id: str,
    draft_patch: dict[str, Any],
    session: Optional[AsyncSession] = None,
) -> Optional[ChatSession]:
    """合并 draft_patch 到现有 draft_request（JSON 合并，覆盖同名键）。"""
    own_session = session is None
    if own_session:
        async with async_session() as session:
            return await update_draft(session_id, draft_patch, session)

    obj = await get_session(session_id, session)
    if not obj:
        return None

    existing = {}
    if obj.draft_request:
        try:
            existing = json.loads(obj.draft_request)
            if not isinstance(existing, dict):
                existing = {}
        except (json.JSONDecodeError, ValueError):
            existing = {}

    existing.update(draft_patch)
    obj.draft_request = json.dumps(existing, ensure_ascii=False)
    # 用户修改草稿后，状态回退为 ready（可重新 apply）
    if obj.status == "consumed":
        obj.status = "ready"
    await session.commit()
    await session.refresh(obj)
    return obj


async def set_pending_confirm(
    session_id: str,
    pending: dict[str, Any],
    session: AsyncSession,
) -> None:
    """存储待确认的写操作（tool_name + tool_args）到 pending_confirm。"""
    obj = await get_session(session_id, session)
    if not obj:
        return
    obj.pending_confirm = json.dumps(pending, ensure_ascii=False)
    await session.commit()


async def get_pending_confirm(
    session_id: str,
    session: AsyncSession,
) -> Optional[dict[str, Any]]:
    """读取待确认的写操作，无则返回 None。"""
    obj = await get_session(session_id, session)
    if not obj or not obj.pending_confirm:
        return None
    try:
        return json.loads(obj.pending_confirm)
    except (json.JSONDecodeError, ValueError):
        return None


async def clear_pending_confirm(
    session_id: str,
    session: AsyncSession,
) -> None:
    """清除待确认的写操作。"""
    obj = await get_session(session_id, session)
    if not obj:
        return
    obj.pending_confirm = None
    await session.commit()


async def set_interpretation(
    session_id: str,
    draft: dict[str, Any],
    confidence: float,
    missing_fields: list[str],
    suggestions: list[str],
    intent: Optional[str] = None,
    session: Optional[AsyncSession] = None,
) -> Optional[ChatSession]:
    """interpret 成功后落库草稿 + 置 status=ready。"""
    own_session = session is None
    if own_session:
        async with async_session() as session:
            return await set_interpretation(
                session_id, draft, confidence, missing_fields, suggestions, intent, session
            )

    obj = await get_session(session_id, session)
    if not obj:
        return None

    obj.draft_request = json.dumps(draft, ensure_ascii=False)
    obj.confidence = confidence
    obj.missing_fields = json.dumps(missing_fields, ensure_ascii=False)
    obj.error_message = None
    obj.status = "ready"
    if intent:
        obj.intent = intent
    await session.commit()
    await session.refresh(obj)
    return obj


async def mark_consumed(
    session_id: str,
    task_uuid: str,
    session: Optional[AsyncSession] = None,
) -> None:
    own_session = session is None
    if own_session:
        async with async_session() as session:
            await mark_consumed(session_id, task_uuid, session)
            return

    obj = await get_session(session_id, session)
    if obj:
        obj.status = "consumed"
        await session.commit()


async def mark_error(
    session_id: str,
    error_message: str,
    session: Optional[AsyncSession] = None,
) -> None:
    own_session = session is None
    if own_session:
        async with async_session() as session:
            await mark_error(session_id, error_message, session)
            return

    obj = await get_session(session_id, session)
    if obj:
        obj.status = "error"
        obj.error_message = error_message
        await session.commit()


async def delete_session(
    session_id: str,
    session: Optional[AsyncSession] = None,
) -> bool:
    """删会话 + 级联删 messages。"""
    own_session = session is None
    if own_session:
        async with async_session() as session:
            return await delete_session(session_id, session)

    # 先删 messages
    await session.execute(
        delete(ChatMessage).where(ChatMessage.session_id == session_id)
    )
    result = await session.execute(
        delete(ChatSession).where(ChatSession.id == session_id)
    )
    await session.commit()
    return (result.rowcount or 0) > 0


async def append_message(
    session_id: str,
    role: str,
    content: Optional[str] = None,
    tool_name: Optional[str] = None,
    tool_args: Optional[dict] = None,
    tool_result: Optional[dict] = None,
    tokens: Optional[int] = None,
    session: Optional[AsyncSession] = None,
) -> ChatMessage:
    own_session = session is None
    if own_session:
        async with async_session() as session:
            return await append_message(
                session_id, role, content, tool_name, tool_args, tool_result, tokens, session
            )

    msg = ChatMessage(
        session_id=session_id,
        role=role,
        content=content,
        tool_name=tool_name,
        tool_args=json.dumps(tool_args, ensure_ascii=False) if tool_args is not None else None,
        tool_result=json.dumps(tool_result, ensure_ascii=False) if tool_result is not None else None,
        tokens=tokens,
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)
    return msg


async def list_messages(
    session_id: str,
    session: Optional[AsyncSession] = None,
) -> list[ChatMessage]:
    own_session = session is None
    if own_session:
        async with async_session() as session:
            return await list_messages(session_id, session)

    result = await session.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
    )
    return list(result.scalars().all())
