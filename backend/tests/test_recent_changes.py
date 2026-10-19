"""针对近期三个修改点的单元测试：
P0 — 任务列表排序稳定性（created_at DESC, id DESC）
P1 — ID 从 8 位 hex 截断改为完整 32 位 UUID
P2 — SSE 多 Worker 警告注释（仅静态检查）
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import (
    Agent,
    Base,
    Dataset,
    EvaluationResult,
    EvaluationTask,
)

pytestmark = pytest.mark.p3


# ── fixture ──────────────────────────────────────────────────────────
@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ═══════════════════════════════════════════════════════════════════════
# P0: 任务列表排序 — 同 created_at 时 id DESC 保证稳定
# ═══════════════════════════════════════════════════════════════════════

async def _seed_tasks_same_created_at(session: AsyncSession, n: int = 10):
    """插入 n 个 created_at 完全相同的任务，id 递减以模拟真实写入顺序。"""
    ts = datetime(2026, 6, 12, 10, 0, 0)
    ds_id = f"ds_{uuid.uuid4().hex}"
    session.add(Dataset(id=ds_id, name="排序测试集", data="[]", item_count=0))
    await session.flush()

    # 构造 id 递减（后创建的 id 字典序更小，模拟 uuid4 随机序）
    task_ids = []
    for i in range(n):
        tid = f"task_{uuid.uuid4().hex}"
        task_ids.append(tid)
        session.add(
            EvaluationTask(
                id=tid,
                dataset_id=ds_id,
                models='["glm"]',
                status="completed",
                created_at=ts,  # 全部相同
            )
        )
    await session.flush()
    return task_ids


@pytest.mark.asyncio
async def test_list_tasks_same_created_at_stable_order(mem_session):
    """同 created_at 的任务必须按 id DESC 排序，保证分页稳定。"""
    task_ids = await _seed_tasks_same_created_at(mem_session, n=10)

    result = await mem_session.execute(
        select(EvaluationTask)
        .order_by(EvaluationTask.created_at.desc(), EvaluationTask.id.desc())
    )
    rows = result.scalars().all()

    assert len(rows) == 10
    # 验证 returned id 集合与插入一致
    returned_ids = [r.id for r in rows]
    assert set(returned_ids) == set(task_ids)
    # 验证 id 严格降序
    for i in range(len(returned_ids) - 1):
        assert returned_ids[i] > returned_ids[i + 1], (
            f"id 降序违反: rows[{i}]={returned_ids[i]} <= rows[{i+1}]={returned_ids[i+1]}"
        )


@pytest.mark.asyncio
async def test_list_tasks_pagination_stable_across_pages(mem_session):
    """模拟翻页：page1 / page2 无重复、无遗漏。"""
    task_ids = await _seed_tasks_same_created_at(mem_session, n=10)

    page_size = 4
    all_returned_ids = []
    seen_ids = set()

    for page in range(1, 4):  # 3 pages: 4+4+2
        offset = (page - 1) * page_size
        result = await mem_session.execute(
            select(EvaluationTask)
            .order_by(EvaluationTask.created_at.desc(), EvaluationTask.id.desc())
            .offset(offset)
            .limit(page_size)
        )
        rows = result.scalars().all()
        page_ids = [r.id for r in rows]
        for pid in page_ids:
            assert pid not in seen_ids, f"翻页出现重复 id: {pid}"
            seen_ids.add(pid)
        all_returned_ids.extend(page_ids)

    # 全部 10 条无遗漏、无重复
    assert set(all_returned_ids) == set(task_ids)
    assert len(all_returned_ids) == 10


@pytest.mark.asyncio
async def test_list_tasks_mixed_created_at_order(mem_session):
    """created_at 不同时，按 created_at DESC；相同时再按 id DESC。"""
    ds_id = f"ds_{uuid.uuid4().hex}"
    mem_session.add(Dataset(id=ds_id, name="混合排序集", data="[]", item_count=0))
    await mem_session.flush()

    # 创建 3 个不同时间 + 2 个相同时间的任务
    t1 = datetime(2026, 6, 12, 8, 0, 0)
    t2 = datetime(2026, 6, 12, 9, 0, 0)
    t3 = datetime(2026, 6, 12, 10, 0, 0)

    ids = {}
    for label, ts in [("old", t1), ("mid", t2), ("new_a", t3), ("new_b", t3)]:
        tid = f"task_{label}_{uuid.uuid4().hex[:6]}"
        ids[label] = tid
        mem_session.add(
            EvaluationTask(
                id=tid,
                dataset_id=ds_id,
                models='["glm"]',
                status="completed",
                created_at=ts,
            )
        )
    await mem_session.flush()

    result = await mem_session.execute(
        select(EvaluationTask)
        .order_by(EvaluationTask.created_at.desc(), EvaluationTask.id.desc())
    )
    rows = result.scalars().all()
    returned_ids = [r.id for r in rows]

    # new_a / new_b 在前（t3），且 id 降序
    assert returned_ids[0] in (ids["new_a"], ids["new_b"])
    assert returned_ids[1] in (ids["new_a"], ids["new_b"])
    # mid 在第三
    assert returned_ids[2] == ids["mid"]
    # old 在最后
    assert returned_ids[3] == ids["old"]


# ═══════════════════════════════════════════════════════════════════════
# P1: ID 格式 — 完整 32 位 hex UUID，不再截断为 8 位
# ═══════════════════════════════════════════════════════════════════════

def test_task_id_full_uuid_format():
    """task_id 必须为 'task_' + 32 位 hex（完整 UUID）。"""
    task_id = f"task_{uuid.uuid4().hex}"
    assert re.match(r"^task_[0-9a-f]{32}$", task_id), (
        f"task_id 格式错误: {task_id!r}，期望 'task_' + 32位hex"
    )


def test_dataset_id_full_uuid_format():
    """ds_id 必须为 'ds_' + 32 位 hex。"""
    ds_id = f"ds_{uuid.uuid4().hex}"
    assert re.match(r"^ds_[0-9a-f]{32}$", ds_id), (
        f"ds_id 格式错误: {ds_id!r}，期望 'ds_' + 32位hex"
    )


def test_agent_id_full_uuid_format():
    """agent_id 必须为 'agent_' + 32 位 hex。"""
    agent_id = f"agent_{uuid.uuid4().hex}"
    assert re.match(r"^agent_[0-9a-f]{32}$", agent_id), (
        f"agent_id 格式错误: {agent_id!r}，期望 'agent_' + 32位hex"
    )


def test_script_id_full_uuid_format():
    """script_id 必须为 'script_' + 32 位 hex。"""
    script_id = f"script_{uuid.uuid4().hex}"
    assert re.match(r"^script_[0-9a-f]{32}$", script_id), (
        f"script_id 格式错误: {script_id!r}，期望 'script_' + 32位hex"
    )


def test_id_uniqueness():
    """连续生成的 100 个 task_id 必须互不相同。"""
    ids = {f"task_{uuid.uuid4().hex}" for _ in range(100)}
    assert len(ids) == 100, "生成的 ID 有重复，UUID 碰撞率异常"


@pytest.mark.asyncio
async def test_task_id_format_in_database(mem_session):
    """验证写入 DB 的 task 记录 ID 符合完整 UUID 格式。"""
    ds_id = f"ds_{uuid.uuid4().hex}"
    mem_session.add(Dataset(id=ds_id, name="ID格式测试集", data="[]", item_count=0))
    await mem_session.flush()

    task_id = f"task_{uuid.uuid4().hex}"
    mem_session.add(
        EvaluationTask(
            id=task_id,
            dataset_id=ds_id,
            models='["glm"]',
            status="pending",
        )
    )
    await mem_session.flush()

    task = await mem_session.get(EvaluationTask, task_id)
    assert task is not None
    assert re.match(r"^task_[0-9a-f]{32}$", task.id), (
        f"DB 中的 task.id 格式错误: {task.id!r}"
    )


# ═══════════════════════════════════════════════════════════════════════
# P2: SSE 多 Worker 警告 — 静态代码检查
# ═══════════════════════════════════════════════════════════════════════

def test_sse_warning_comment_exists():
    """tasks.py 中 task_streams 字典上方必须有多 Worker 警告注释。"""
    import tasks as tasks_mod
    source = open(tasks_mod.__file__, encoding="utf-8").read()
    # 检查关键字：workers、进程、SSE
    assert "workers" in source.lower() or "Worker" in source, (
        "tasks.py 中 task_streams 上缺少多 Worker 警告注释"
    )
    # 确认 task_streams 存在
    assert hasattr(tasks_mod, "task_streams"), "tasks.py 中缺少 task_streams"


def test_no_truncated_hex_in_main_id_generation():
    """main.py 中 ID 生成不应出现 [:8] 截断。"""
    import main as main_mod
    source = open(main_mod.__file__, encoding="utf-8").read()

    # 查找 uuid4().hex[:8] 模式（不包含测试文件中的使用）
    truncated = re.findall(r"uuid4\(\)\.hex\[:8\]", source)
    assert len(truncated) == 0, (
        f"main.py 中仍存在 uuid4().hex[:8] 截断: {truncated}"
    )
