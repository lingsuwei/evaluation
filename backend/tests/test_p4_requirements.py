"""P4 需求追溯 + 契约 + 异常路径验证（测试专家补充套件）。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from comparison_routes import comparison_router
from database import AgentRun, Base, RunDiff, get_session
from run_routes import run_router
from services.eval_context import build_eval_context
from services.run_comparison_service import compare_runs
from services.span_tree_service import build_nested_tree, build_span_tree_for_run
from tests.test_p3_diff_calculators import _seed_runs

pytestmark = pytest.mark.p4


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def app(mem_session):
    app = FastAPI()
    app.include_router(run_router, prefix="/api")
    app.include_router(comparison_router, prefix="/api")

    async def _override():
        yield mem_session

    app.dependency_overrides[get_session] = _override
    return app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# --- P4-REQ-01: API 契约（§10.3.3 span_tree_ui） ---


@pytest.mark.asyncio
async def test_span_tree_response_contract(client, mem_session):
    """GET /runs/{id}/span-tree 应含扁平 nodes + 嵌套 tree + 元数据。"""
    await _seed_runs(mem_session)
    resp = await client.get("/api/runs/run_left/span-tree")
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "agent_run_id",
        "trace_id",
        "agent_id",
        "task_id",
        "question_id",
        "nodes",
        "tree",
        "node_count",
    ):
        assert key in body, f"missing contract field: {key}"
    assert body["agent_run_id"] == "run_left"
    assert isinstance(body["nodes"], list)
    assert isinstance(body["tree"], list)
    assert body["node_count"] == len(body["nodes"])
    node = body["nodes"][0]
    for field in ("id", "name", "aggregate_key", "latency_ms", "tokens", "children"):
        assert field in node


@pytest.mark.asyncio
async def test_dual_span_trees_response_contract(client, mem_session):
    """GET /comparisons/{id}/span-trees 应含双树 + 对齐集 + 附带 diffs。"""
    await _seed_runs(mem_session)
    cmp = await compare_runs(
        mem_session, left_run_id="run_left", right_run_id="run_right"
    )
    resp = await client.get(f"/api/comparisons/{cmp['id']}/span-trees")
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "comparison_id",
        "left_run_id",
        "right_run_id",
        "left",
        "right",
        "align_aggregate_keys",
        "only_left_aggregate_keys",
        "only_right_aggregate_keys",
        "diffs",
    ):
        assert key in body, f"missing contract field: {key}"
    assert body["left"]["node_count"] == 2
    assert body["right"]["node_count"] == 1
    assert "step:plan" in body["align_aggregate_keys"]
    assert "tool:search" in body["only_left_aggregate_keys"]


# --- P4-REQ-02: 异常路径 HTTP 404 ---


@pytest.mark.asyncio
async def test_run_span_tree_api_404(client):
    resp = await client.get("/api/runs/nonexistent/span-tree")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_comparison_span_trees_api_404(client):
    resp = await client.get("/api/comparisons/cmp_missing/span-trees")
    assert resp.status_code == 404


# --- P4-REQ-03: 路由顺序（span-trees 不得被 /{id} 吞掉） ---


@pytest.mark.asyncio
async def test_span_trees_route_not_shadowed_by_detail(client, mem_session):
    await _seed_runs(mem_session)
    cmp = await compare_runs(
        mem_session, left_run_id="run_left", right_run_id="run_right"
    )
    resp = await client.get(f"/api/comparisons/{cmp['id']}/span-trees")
    assert resp.status_code == 200
    assert "left" in resp.json() and "right" in resp.json()


# --- P4-REQ-04: aggregate_key 对齐规则（unknown 排除） ---


def test_unknown_aggregate_key_excluded_from_align():
    from services.span_tree_service import collect_aggregate_keys

    keys = collect_aggregate_keys(
        [
            {"aggregate_key": "step:plan"},
            {"aggregate_key": "unknown"},
            {"aggregate_key": ""},
            {"aggregate_key": "  "},
        ]
    )
    assert keys == {"step:plan"}


# --- P4-REQ-05: trace_id 回退查询 ---


@pytest.mark.asyncio
async def test_span_tree_lookup_by_trace_id(mem_session):
    await _seed_runs(mem_session)
    run = await mem_session.get(AgentRun, "run_left")
    run.trace_id = "trace_abc_left"
    await mem_session.commit()
    tree = await build_span_tree_for_run(mem_session, "trace_abc_left")
    assert tree is not None
    assert tree["agent_run_id"] == "run_left"


# --- P4-REQ-06: 空 Span 树（Run 存在但无节点） ---


@pytest.mark.asyncio
async def test_empty_span_tree_for_run_without_nodes(mem_session):
    started = datetime.utcnow()
    mem_session.add(
        AgentRun(
            id="run_empty",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t1", dataset_id="d1", question_id=0, run_index=0
            ),
            task_id="t1",
            dataset_id="d1",
            question_id=0,
            agent_id="ag_x",
            status="success",
            started_at=started,
            ended_at=started + timedelta(seconds=1),
            tokens_total=0,
            agent_snapshot="{}",
            trace_sync_status="synced",
        )
    )
    await mem_session.commit()
    tree = await build_span_tree_for_run(mem_session, "run_empty")
    assert tree is not None
    assert tree["node_count"] == 0
    assert tree["tree"] == []


# --- P4-REQ-07: 嵌套树 sibling_order 排序 ---


def test_nested_tree_children_sorted_by_sibling_order():
    flat = [
        {"id": "root", "parent_id": None, "sibling_order": 0, "step_index": 0, "children": []},
        {"id": "c2", "parent_id": "root", "sibling_order": 2, "step_index": 1, "children": []},
        {"id": "c1", "parent_id": "root", "sibling_order": 1, "step_index": 2, "children": []},
    ]
    tree = build_nested_tree(flat)
    child_ids = [c["id"] for c in tree[0]["children"]]
    assert child_ids == ["c1", "c2"]


# --- P4-REQ-08: span_tree_ui 只读，不落库 diff ---


@pytest.mark.asyncio
async def test_span_tree_reads_do_not_create_run_diffs(client, mem_session):
    await _seed_runs(mem_session)
    cmp = await compare_runs(
        mem_session, left_run_id="run_left", right_run_id="run_right"
    )
    before = (
        await mem_session.execute(select(func.count()).select_from(RunDiff))
    ).scalar_one()
    await client.get("/api/runs/run_left/span-tree")
    await client.get(f"/api/comparisons/{cmp['id']}/span-trees")
    after = (
        await mem_session.execute(select(func.count()).select_from(RunDiff))
    ).scalar_one()
    assert after == before
