"""
独立验证脚本：测试 trace 物化三级递进查找 + 边界 + 异常。
不依赖 pytest / sqlalchemy / fastapi，仅用标准库 unittest.mock + asyncio。
"""
import sys
import os
import types
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 1. Stub 第三方依赖 ──────────────────────────────────────
# 在 import 项目模块之前，注入 stub 模块到 sys.modules

def _stub_module(name, attrs=None):
    mod = types.ModuleType(name)
    if attrs:
        for k, v in attrs.items():
            setattr(mod, k, v)
    sys.modules[name] = mod
    return mod

# sqlalchemy stub
sa = types.ModuleType("sqlalchemy")
sa.Column = MagicMock()
sa.String = MagicMock(return_value=MagicMock())
sa.Integer = MagicMock(return_value=MagicMock())
sa.Boolean = MagicMock(return_value=MagicMock())
sa.Text = MagicMock(return_value=MagicMock())
sa.Float = MagicMock(return_value=MagicMock())
sa.DateTime = MagicMock(return_value=MagicMock())
sa.JSON = MagicMock(return_value=MagicMock())
sa.select = MagicMock()
sa.text = MagicMock()
sa.Index = MagicMock()
sa.ForeignKey = MagicMock()
sa.UniqueConstraint = MagicMock()
sa.ForeignKeyConstraint = MagicMock()

sa_ext = types.ModuleType("sqlalchemy.ext")
sa_async = types.ModuleType("sqlalchemy.ext.asyncio")
sa_async.AsyncSession = MagicMock()
sa_async.create_async_engine = MagicMock()
sa_ext.asyncio = sa_async
sys.modules["sqlalchemy"] = sa
sys.modules["sqlalchemy.ext"] = sa_ext
sys.modules["sqlalchemy.ext.asyncio"] = sa_async

sa_orm = types.ModuleType("sqlalchemy.orm")
sa_orm.declarative_base = MagicMock(return_value=MagicMock())
sa_orm.sessionmaker = MagicMock()
sys.modules["sqlalchemy.orm"] = sa_orm

# fastapi stub
_stub_module("fastapi", {"FastAPI": MagicMock, "BackgroundTasks": MagicMock, "HTTPException": Exception})

# pydantic stub
_stub_module("pydantic", {"BaseModel": MagicMock})

# httpx stub
httpx = _stub_module("httpx", {
    "AsyncClient": MagicMock,
    "Timeout": MagicMock,
    "TimeoutException": Exception,
    "HTTPStatusError": Exception,
})

# 其他 stub
for mod_name in [
    "dotenv", "redis", "alembic", "alembic.config", "alembic.command",
    "alembic.script", "alembic.runtime.environment",
    "httpx._utils", "httpx._client",
]:
    _stub_module(mod_name)

# ── 2. 注入项目级 stub ──────────────────────────────────────
# config
config_mod = _stub_module("config", {
    "LANGFUSE_ENABLED": True,
    "LANGFUSE_BASE_URL": "http://localhost:3000",
    "LANGFUSE_PUBLIC_KEY": "pk-test",
    "LANGFUSE_SECRET_KEY": "sk-test",
    "TRACE_SYNC_WORKER_ENABLED": True,
    "TRACE_SYNC_POLL_INTERVAL_SEC": 10.0,
    "TRACE_SYNC_BATCH_SIZE": 20,
    "TRACE_SYNC_MAX_ATTEMPTS": 10,
    "TRACE_SYNC_MIN_INGESTION_DELAY_SEC": 60,
    "TRACE_SYNC_STABILITY_WINDOW_SEC": 60,
    "TRACE_SYNC_MAX_WAIT_SEC": 300,
    "LANGFUSE_WORKER_TIMEOUT": 30.0,
    "DATABASE_URL": "sqlite:///test.db",
    "DB_POOL_SIZE": 5,
    "DB_MAX_OVERFLOW": 10,
    "DB_POOL_RECYCLE": 3600,
    "ENV_LABEL": "test",
    "LANGFUSE_PROJECTS": {},
    "LANGFUSE_DEFAULT_PROJECT": "default",
    "TRACE_SYNC_MAX_RERUN": 3,
    "TRACE_SYNC_SWEEP_INTERVAL_SEC": 60,
    "TRACE_SYNC_SWEEP_BATCH_SIZE": 5,
    "TRACE_SYNC_INCOMPLETE_LOOKBACK_HOURS": 24,
})

# logger
logger_mod = _stub_module("logger", {"log": MagicMock()})

# database stubs
db_mod = _stub_module("database")
# AgentRun 列属性用 MagicMock 以支持 .isnot()/== 等表达式
_col = MagicMock()
db_mod.AgentRun = type("AgentRun", (), {
    "id": _col, "status": None, "trace_id": _col, "root_trace_id": None,
    "started_at": None, "ended_at": None, "onto_agent_run_id": None,
    "onto_agent_session_id": None, "langfuse_project": None,
    "trace_sync_status": None, "model_answer": None,
    "task_id": _col, "failure_category": None,
})
db_mod.TraceSyncJob = type("TraceSyncJob", (), {
    "id": None, "agent_run_id": None, "trace_id": None, "status": None,
    "attempts": 0, "created_at": None, "processed_at": None,
    "last_error": None, "langfuse_project": None,
    "failure_category": None,
    "rerun_count": 0, "incomplete_metadata": None,
})
db_mod.async_session = MagicMock()
db_mod.Base = MagicMock()
db_mod.init_database = AsyncMock()
db_mod.get_session = AsyncMock()

# langfuse_client stub
lc_mod = _stub_module("services.langfuse_client")
lc_mod.LANGFUSE_WORKER_TIMEOUT = 30.0
lc_mod.LangfuseClient = MagicMock
lc_mod.get_langfuse_client = MagicMock(return_value=MagicMock())

# langfuse_trace_filter stub
ltf_mod = _stub_module("services.langfuse_trace_filter")
ltf_mod.filter_out_eval_reported_traces = lambda traces: traces

# langfuse_project_registry stub
lpr_mod = _stub_module("services.langfuse_project_registry")
class _ProjectNotFoundError(Exception):
    pass
lpr_mod.ProjectNotFoundError = _ProjectNotFoundError

# langfuse_trace_resolver stub — imported by trace_sync_worker but we test it separately
# (real module loaded below via sys.path; stub prevents circular import issues)

# trace_sync_failure_category stub
tsfc_mod = _stub_module("services.trace_sync_failure_category")
tsfc_mod.classify_failure = lambda e: "unknown"

# trace_ref_service stub
trs_mod = _stub_module("services.trace_ref_service")
trs_mod.ensure_root_trace_ref = AsyncMock()

# runtime_fact_builder stub
rfb_mod = _stub_module("services.runtime_fact_builder")
rfb_mod.build_facts_for_trace = AsyncMock(return_value={})
rfb_mod.count_observations = lambda trace: len(trace.get("observations", []))

# trace_sync_observability stub
tso_mod = _stub_module("services.trace_sync_observability")
tso_mod.get_trace_sync_queue_stats = AsyncMock(return_value=MagicMock(as_dict=lambda: {}))

# agent_run_service stub (for get_agent_run)
ars_mod = _stub_module("services.agent_run_service")
ars_mod.complete_agent_run = AsyncMock()
ars_mod.get_agent_run = AsyncMock()

# services package — 必须是 package（有 __path__）
_services_mod = types.ModuleType("services")
_services_mod.__path__ = [os.path.join(_BACKEND_DIR, "services")]
sys.modules["services"] = _services_mod

# ── 3. 导入被测模块 ──────────────────────────────────────
sys.path.insert(0, _BACKEND_DIR)

from services.langfuse_trace_resolver import (
    find_trace_by_time_window,
    find_traces_by_run_windows,
    _parse_ts,
    _TIME_WINDOW_PADDING_SECONDS,
)
from services.trace_sync_worker import (
    _find_trace_by_session,
    process_trace_sync_job,
    drain_pending_trace_sync_jobs,
    _defer_incomplete_materialization,
    _sweep_incomplete_materialization,
)

# ── 4. 测试用例 ──────────────────────────────────────────

# 简易 async 标记（无 pytest 依赖）
def pytest_mark_asyncio(func):
    """标记 async 测试函数"""
    return func


def make_trace(tid, ts_str, metadata=None):
    return {"id": tid, "timestamp": ts_str, "metadata": metadata or {}}

def make_run(started_at, ended_at=None, session_id=None, run_id=None, trace_id=None, task_id=None):
    run = MagicMock()
    run.started_at = started_at
    run.ended_at = ended_at
    run.onto_agent_session_id = session_id
    run.onto_agent_run_id = run_id
    run.trace_id = trace_id
    run.langfuse_project = "default"
    run.task_id = task_id
    return run

def make_job(trace_id="trace-1", created_at=None, attempts=0):
    job = MagicMock()
    job.id = 1
    job.trace_id = trace_id
    job.created_at = created_at or datetime.utcnow()
    job.attempts = attempts
    job.status = "pending"
    job.langfuse_project = "default"
    job.agent_run_id = "run-1"
    job.failure_category = None
    job.rerun_count = 0
    job.incomplete_metadata = None
    return job

def make_async_session():
    """创建带 AsyncMock flush/commit 的 session"""
    session = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    return session


class TestParseTs:
    """_parse_ts 边界测试"""

    def test_valid_iso(self):
        dt = _parse_ts("2025-01-01T12:00:00Z")
        assert dt is not None
        assert dt.year == 2025

    def test_valid_with_tz(self):
        dt = _parse_ts("2025-01-01T12:00:00+08:00")
        assert dt is not None

    def test_none_input(self):
        assert _parse_ts(None) is None

    def test_empty_string(self):
        assert _parse_ts("") is None

    def test_garbage(self):
        assert _parse_ts("not-a-date") is None

    def test_naive_datetime(self):
        dt = _parse_ts("2025-01-01T12:00:00")
        assert dt is not None
        assert dt.tzinfo is not None  # 应自动补 UTC


class TestTimeWindowPadding:
    """时间窗 ±180s 验证"""

    def test_padding_is_180(self):
        assert _TIME_WINDOW_PADDING_SECONDS == 180


class TestFindTraceByTimeWindow:
    """find_trace_by_time_window 测试"""

    @pytest_mark_asyncio
    async def test_basic_match(self):
        """正常匹配：一条 trace 在窗口内"""
        client = MagicMock()
        anchor = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        trace = make_trace("t1", anchor.isoformat())
        client.list_traces = AsyncMock(return_value={"data": [trace], "meta": {"totalPages": 1}})

        result = await find_trace_by_time_window(client, anchor, "test")
        assert result is not None
        assert result["id"] == "t1"

    @pytest_mark_asyncio
    async def test_no_traces(self):
        """窗口内无 trace"""
        client = MagicMock()
        anchor = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        client.list_traces = AsyncMock(return_value={"data": [], "meta": {"totalPages": 1}})

        result = await find_trace_by_time_window(client, anchor, "test")
        assert result is None

    @pytest_mark_asyncio
    async def test_invalid_anchor(self):
        """anchor_ts 无法解析"""
        client = MagicMock()
        result = await find_trace_by_time_window(client, "garbage", "test")
        assert result is None

    @pytest_mark_asyncio
    async def test_claimed_trace_ids(self):
        """已被占用的 trace 被跳过"""
        client = MagicMock()
        anchor = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t1 = make_trace("t1", anchor.isoformat())
        t2 = make_trace("t2", (anchor + timedelta(seconds=1)).isoformat())
        client.list_traces = AsyncMock(return_value={"data": [t1, t2], "meta": {"totalPages": 1}})

        # t1 已被占用 → 应返回 t2
        result = await find_trace_by_time_window(
            client, anchor, "test", claimed_trace_ids={"t1"}
        )
        assert result is not None
        assert result["id"] == "t2"

    @pytest_mark_asyncio
    async def test_end_ts_extends_window(self):
        """end_ts 扩展窗口上限，覆盖长推理"""
        client = MagicMock()
        start = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(minutes=30)
        # trace 在推理中间创建（start + 15min）
        trace_ts = start + timedelta(minutes=15)
        trace = make_trace("t1", trace_ts.isoformat())
        client.list_traces = AsyncMock(return_value={"data": [trace], "meta": {"totalPages": 1}})

        # 不传 end_ts：±180s 窗口不覆盖 +15min
        result_no_end = await find_trace_by_time_window(client, start, "test")
        # list_traces 被调用，窗口 from/to 的范围不包含 trace_ts
        call_args = client.list_traces.call_args
        from_ts = call_args.kwargs["from_timestamp"]
        to_ts = call_args.kwargs["to_timestamp"]
        # to_ts = start + 180s = start + 3min，trace_ts = start + 15min → 窗口外
        # 但 list_traces 可能仍返回 trace（取决于 Langfuse 端实现），这里只验证窗口参数正确
        assert "2025-01-01T11:57" in from_ts
        assert "2025-01-01T12:03" in to_ts  # +180s

        # 传 end_ts：窗口覆盖到 end + 180s
        result_with_end = await find_trace_by_time_window(
            client, start, "test", end_ts=end
        )
        call_args2 = client.list_traces.call_args
        to_ts2 = call_args2.kwargs["to_timestamp"]
        assert "2025-01-01T12:33" in to_ts2  # end + 180s

    @pytest_mark_asyncio
    async def test_list_traces_exception(self):
        """list_traces 异常 → 返回 None"""
        client = MagicMock()
        client.list_traces = AsyncMock(side_effect=Exception("network error"))
        anchor = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = await find_trace_by_time_window(client, anchor, "test")
        assert result is None

    @pytest_mark_asyncio
    async def test_session_id_passed_to_list_traces(self):
        """session_id 正确传递给 list_traces"""
        client = MagicMock()
        anchor = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        client.list_traces = AsyncMock(return_value={"data": [], "meta": {"totalPages": 1}})

        await find_trace_by_time_window(client, anchor, "test", session_id="sess-123")
        call_args = client.list_traces.call_args
        assert call_args.kwargs.get("session_id") == "sess-123"


class TestFindTraceBySession:
    """_find_trace_by_session (Level 2) 测试"""

    @pytest_mark_asyncio
    async def test_no_session_id(self):
        """run 没有 onto_agent_session_id → 返回 None"""
        client = MagicMock()
        run = make_run(datetime.utcnow(), session_id=None)
        result = await _find_trace_by_session(client, run)
        assert result is None

    @pytest_mark_asyncio
    async def test_empty_session_id(self):
        """session_id 为空字符串 → 返回 None"""
        client = MagicMock()
        run = make_run(datetime.utcnow(), session_id="")
        result = await _find_trace_by_session(client, run)
        assert result is None

    @pytest_mark_asyncio
    async def test_exact_run_id_match(self):
        """metadata.run_id 精确匹配"""
        client = MagicMock()
        start = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t1 = make_trace("t1", start.isoformat(), metadata={"run_id": "run-0"})
        t2 = make_trace("t2", start.isoformat(), metadata={"run_id": "run-1"})
        client.list_traces = AsyncMock(return_value={"data": [t1, t2]})

        run = make_run(start, session_id="sess-1", run_id="run-1")
        result = await _find_trace_by_session(client, run)
        assert result is not None
        assert result["id"] == "t2"

    @pytest_mark_asyncio
    async def test_no_run_id_match_fallback_to_timestamp(self):
        """trace 无 run_id → 按时间戳近似（生产逻辑：只对无 run_id 的 trace 做时间戳 fallback，
        run_id 明确不匹配的 trace 会被跳过）"""
        client = MagicMock()
        start = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(minutes=10)
        # trace 不带 run_id（模拟 Langfuse 未回传 run_id 的场景）
        t1 = make_trace("t1", (start + timedelta(seconds=30)).isoformat(), metadata={})
        t2 = make_trace("t2", (start + timedelta(seconds=120)).isoformat(), metadata={})
        client.list_traces = AsyncMock(return_value={"data": [t1, t2]})

        run = make_run(start, ended_at=end, session_id="sess-1", run_id="run-1")
        result = await _find_trace_by_session(client, run)
        # t1 落在 [start, end] 区间内 → diff=0，最高优先级
        assert result is not None
        assert result["id"] == "t1"

    @pytest_mark_asyncio
    async def test_list_traces_exception(self):
        """list_traces 异常 → 返回 None"""
        client = MagicMock()
        client.list_traces = AsyncMock(side_effect=Exception("API error"))
        run = make_run(datetime.utcnow(), session_id="sess-1")
        result = await _find_trace_by_session(client, run)
        assert result is None

    @pytest_mark_asyncio
    async def test_no_traces_returned(self):
        """session_id 查询返回空列表"""
        client = MagicMock()
        client.list_traces = AsyncMock(return_value={"data": []})
        run = make_run(datetime.utcnow(), session_id="sess-1")
        result = await _find_trace_by_session(client, run)
        assert result is None


class TestProcessTraceSyncJob:
    """process_trace_sync_job 边界测试"""

    @pytest_mark_asyncio
    async def test_deferred_too_young(self):
        """job 创建不到 60s → deferred"""
        import services.trace_sync_worker as worker_mod
        with patch.object(worker_mod, "TRACE_SYNC_MIN_INGESTION_DELAY_SEC", 60):
            session = make_async_session()
            job = make_job(created_at=datetime.utcnow(), attempts=0)
            result = await process_trace_sync_job(session, job)
            assert result["status"] == "deferred"

    @pytest_mark_asyncio
    async def test_deferred_zero_delay(self):
        """MIN_INGESTION_DELAY_SEC=0 → 不 defer"""
        import services.trace_sync_worker as worker_mod
        with patch.object(worker_mod, "TRACE_SYNC_MIN_INGESTION_DELAY_SEC", 0):
            session = make_async_session()
            job = make_job(trace_id="", created_at=datetime.utcnow(), attempts=0)
            result = await process_trace_sync_job(session, job)
            # trace_id 为空 → skipped
            assert result["status"] == "skipped"

    @pytest_mark_asyncio
    async def test_empty_trace_id_skipped(self):
        """trace_id 为空 → skipped"""
        import services.trace_sync_worker as worker_mod
        with patch.object(worker_mod, "TRACE_SYNC_MIN_INGESTION_DELAY_SEC", 0):
            session = make_async_session()
            job = make_job(trace_id="  ", attempts=0)
            result = await process_trace_sync_job(session, job)
            assert result["status"] == "skipped"

    @pytest_mark_asyncio
    async def test_max_attempts_exceeded(self):
        """attempts >= MAX_ATTEMPTS → failed"""
        import services.trace_sync_worker as worker_mod
        with patch.object(worker_mod, "TRACE_SYNC_MIN_INGESTION_DELAY_SEC", 0), \
             patch.object(worker_mod, "TRACE_SYNC_MAX_ATTEMPTS", 3), \
             patch.object(worker_mod, "LANGFUSE_ENABLED", True), \
             patch.object(worker_mod, "get_agent_run", AsyncMock(return_value=make_run(datetime.utcnow()))), \
             patch.object(worker_mod, "get_langfuse_client", MagicMock(return_value=MagicMock())), \
             patch.object(worker_mod, "build_facts_for_trace", AsyncMock(side_effect=Exception("fail"))), \
             patch.object(worker_mod, "_check_observation_stability", return_value=(False, "test", None)):
            session = make_async_session()
            job = make_job(trace_id="trace-1", attempts=3)
            result = await process_trace_sync_job(session, job)
            assert result["status"] == "failed"

    @pytest_mark_asyncio
    async def test_langfuse_disabled(self):
        """LANGFUSE_ENABLED=False → failed"""
        import services.trace_sync_worker as worker_mod
        with patch.object(worker_mod, "TRACE_SYNC_MIN_INGESTION_DELAY_SEC", 0), \
             patch.object(worker_mod, "LANGFUSE_ENABLED", False):
            session = make_async_session()
            job = make_job(trace_id="trace-1", attempts=0)
            result = await process_trace_sync_job(session, job)
            assert result["status"] == "failed"
            assert "Langfuse" in result.get("error", "")


class TestBackwardCompatibility:
    """向后兼容性验证"""

    @pytest_mark_asyncio
    async def test_find_trace_no_new_params(self):
        """不传 session_id/claimed_trace_ids/end_ts → 正常工作"""
        client = MagicMock()
        anchor = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        trace = make_trace("t1", anchor.isoformat())
        client.list_traces = AsyncMock(return_value={"data": [trace], "meta": {"totalPages": 1}})

        # 模拟 inference_service.py 的调用方式：只传 client, anchor_ts, project_label
        result = await find_trace_by_time_window(client, anchor, "proj")
        assert result is not None
        assert result["id"] == "t1"

    @pytest_mark_asyncio
    async def test_process_trace_sync_job_no_claimed_trace_ids(self):
        """不传 claimed_trace_ids → 正常工作（backfill 调用方式）"""
        import services.trace_sync_worker as worker_mod
        with patch.object(worker_mod, "TRACE_SYNC_MIN_INGESTION_DELAY_SEC", 0), \
             patch.object(worker_mod, "LANGFUSE_ENABLED", True), \
             patch.object(worker_mod, "get_agent_run", AsyncMock(return_value=make_run(datetime.utcnow()))), \
             patch.object(worker_mod, "get_langfuse_client", MagicMock(return_value=MagicMock())), \
             patch.object(worker_mod, "build_facts_for_trace", AsyncMock(return_value={})), \
             patch.object(worker_mod, "_check_observation_stability", return_value=(False, "test", None)):
            # mock get_trace 成功
            worker_mod.get_langfuse_client.return_value.get_trace = AsyncMock(
                return_value=make_trace("t1", datetime.utcnow().isoformat())
            )
            session = make_async_session()
            job = make_job(trace_id="trace-1", attempts=0)
            # 不传 claimed_trace_ids
            result = await process_trace_sync_job(session, job)
            assert result["status"] == "success"


class TestCrossDrainTraceIdDedup:
    """跨 drain 周期 trace_id 去重回归测试。

    复现场景：同一 task 的两个 run（run_A / run_B）在并行多轮推理中，
    stable_langfuse_trace_id 推导出相同的 trace_id（因输入哈希相同或
    session_id 缺失）。run_A 先处理成功，trace_id="trace_X" 写入 DB。
    run_B 的 job.trace_id 也是 "trace_X"，但在另一个 drain 周期处理时，
    claimed_trace_ids 已被重置（drain-local），导致 Level 1 直查命中
    trace_X → run_B 也绑定到同一条 trace。

    修复：用 DB 查询同 task 其他 run 已占用的 trace_id，若 job.trace_id
    在其中，跳过 Level 1，降级到 Level 2/3 重新定位。
    """

    @pytest_mark_asyncio
    async def test_level1_skipped_when_trace_id_claimed_by_same_task_run(self):
        """job.trace_id 已被同 task 其他 run 占用 → Level 1 跳过，Level 2 命中不同 trace。"""
        import services.trace_sync_worker as worker_mod

        start = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        # run_B: task_id="task_1", session_id="sess-1", run_id="run-B"
        # job.trace_id = "trace_X"（与 run_A 相同，需被去重）
        run_b = make_run(
            start, ended_at=start + timedelta(minutes=5),
            session_id="sess-1", run_id="run-B", trace_id="trace_X",
            task_id="task_1",
        )
        job = make_job(trace_id="trace_X", attempts=0)

        # DB 查询返回 run_A 已占用的 trace_id
        db_result = MagicMock()
        db_result.scalars.return_value = ["trace_X"]
        session = make_async_session()
        session.execute = AsyncMock(return_value=db_result)

        # trace_X: run_A 的 trace（不应被 run_B 绑定）
        # trace_Y: run_B 的正确 trace（Level 2 命中）
        trace_x = make_trace("trace_X", start.isoformat(), metadata={"run_id": "run-A"})
        trace_y = make_trace("trace_Y", (start + timedelta(seconds=10)).isoformat(),
                             metadata={"run_id": "run-B"})

        client = MagicMock()
        # get_trace: trace_X 返回 trace_x, trace_Y 返回 trace_y（含 observations）
        async def _get_trace(tid, timeout=None):
            if tid == "trace_X":
                return trace_x
            elif tid == "trace_Y":
                return {**trace_y, "observations": []}
            return None
        client.get_trace = AsyncMock(side_effect=_get_trace)
        # list_traces: Level 2 按 session_id 查询，返回 trace_Y
        client.list_traces = AsyncMock(return_value={"data": [trace_y]})

        with patch.object(worker_mod, "TRACE_SYNC_MIN_INGESTION_DELAY_SEC", 0), \
             patch.object(worker_mod, "LANGFUSE_ENABLED", True), \
             patch.object(worker_mod, "get_agent_run", AsyncMock(return_value=run_b)), \
             patch.object(worker_mod, "get_langfuse_client", MagicMock(return_value=client)), \
             patch.object(worker_mod, "build_facts_for_trace", AsyncMock(return_value={})), \
             patch.object(worker_mod, "_check_observation_stability", return_value=(False, "test", None)):
            result = await process_trace_sync_job(session, job)

        # 验证：Level 1 被跳过，Level 2 命中 trace_Y
        assert result["status"] == "success"
        assert job.trace_id == "trace_Y"
        assert run_b.trace_id == "trace_Y"

        # get_trace 不应被以 "trace_X" 调用（Level 1 被 RuntimeError 跳过）
        trace_ids_called = [call.args[0] if call.args else call.kwargs.get("trace_id")
                           for call in client.get_trace.call_args_list]
        assert "trace_X" not in trace_ids_called, \
            f"Level 1 不应直查 trace_X，实际调用: {trace_ids_called}"
        assert "trace_Y" in trace_ids_called


class TestIncompleteMaterializationDeferral:
    """_defer_incomplete_materialization 独立计数重试测试"""

    @pytest_mark_asyncio
    async def test_defer_incomplete_first_two_reruns(self):
        """前 2 次 rerun → deferred_incomplete，job 回 pending"""
        import json
        import services.trace_sync_worker as worker_mod
        with patch.object(worker_mod, "TRACE_SYNC_MAX_RERUN", 3):
            session = make_async_session()
            run = make_run(datetime.utcnow())
            run.id = "run-defer-1"
            now = datetime.utcnow()
            incomplete = {"reason": "zero_spans_with_observations", "obs_count_at_materialization": 152}

            # 第 1 次
            job = make_job(trace_id="trace-inc-1")
            result = await _defer_incomplete_materialization(session, job, run, now, incomplete)
            assert result["status"] == "deferred_incomplete"
            assert result["rerun_count"] == 1
            assert job.status == "pending"
            assert job.processed_at is None
            assert run.trace_sync_status == "pending_sync"
            assert job.failure_category == "incomplete_materialization"

            # 第 2 次（同一 job，模拟下轮 drain 处理后再次进入 defer）
            job.rerun_count = 1  # 模拟上轮已 +1
            result = await _defer_incomplete_materialization(session, job, run, now, incomplete)
            assert result["status"] == "deferred_incomplete"
            assert result["rerun_count"] == 2
            assert job.status == "pending"

    @pytest_mark_asyncio
    async def test_defer_incomplete_max_rerun_marks_synced_incomplete(self):
        """第 3 次 rerun（>= MAX_RERUN）→ synced_incomplete 终态"""
        import json
        import services.trace_sync_worker as worker_mod
        with patch.object(worker_mod, "TRACE_SYNC_MAX_RERUN", 3):
            session = make_async_session()
            run = make_run(datetime.utcnow())
            run.id = "run-defer-2"
            now = datetime.utcnow()
            incomplete = {"reason": "zero_spans_with_observations", "obs_count_at_materialization": 179}

            job = make_job(trace_id="trace-inc-2")
            job.rerun_count = 2  # 模拟前 2 次已完成
            result = await _defer_incomplete_materialization(session, job, run, now, incomplete)
            assert result["status"] == "synced_incomplete"
            assert result["rerun_count"] == 3
            assert job.status == "success"
            assert job.processed_at == now
            assert run.trace_sync_status == "synced_incomplete"
            assert job.failure_category == "incomplete_materialization"

    @pytest_mark_asyncio
    async def test_defer_incomplete_writes_metadata(self):
        """incomplete_metadata 正确写入 JSON"""
        import json
        import services.trace_sync_worker as worker_mod
        with patch.object(worker_mod, "TRACE_SYNC_MAX_RERUN", 3):
            session = make_async_session()
            run = make_run(datetime.utcnow())
            run.id = "run-defer-3"
            now = datetime.utcnow()
            incomplete = {"reason": "zero_spans_with_observations", "obs_count_at_materialization": 200}

            job = make_job(trace_id="trace-inc-3")
            await _defer_incomplete_materialization(session, job, run, now, incomplete)

            # incomplete_metadata 是 JSON 字符串
            assert job.incomplete_metadata is not None
            meta = json.loads(job.incomplete_metadata)
            assert meta["reason"] == "zero_spans_with_observations"
            assert meta["obs_count_at_materialization"] == 200


class TestSweepIncompleteMaterialization:
    """_sweep_incomplete_materialization 补扫测试"""

    @pytest_mark_asyncio
    async def test_sweep_rate_limited(self):
        """60s 内第二次调用 → 返回 0（限频跳过）"""
        import time
        import services.trace_sync_worker as worker_mod
        # 重置限频时间戳
        worker_mod._last_sweep_at = 0.0
        with patch.object(worker_mod, "TRACE_SYNC_SWEEP_INTERVAL_SEC", 60), \
             patch.object(worker_mod, "async_session") as mock_session_factory:
            # 第一次调用：_last_sweep_at 被设为当前时间
            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)
            mock_session.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))
            mock_session_factory.return_value = mock_session

            count1 = await _sweep_incomplete_materialization()
            # 无数据时返回 0，但 _last_sweep_at 已更新
            assert count1 == 0

            # 第二次调用：应被限频跳过
            count2 = await _sweep_incomplete_materialization()
            assert count2 == 0
            # 限频时不应再开 session
            assert mock_session_factory.call_count == 1

    @pytest_mark_asyncio
    async def test_sweep_skips_run_with_active_job(self):
        """有 pending/running job 的 run → 跳过"""
        import time
        import services.trace_sync_worker as worker_mod
        worker_mod._last_sweep_at = 0.0
        with patch.object(worker_mod, "TRACE_SYNC_SWEEP_INTERVAL_SEC", 60), \
             patch.object(worker_mod, "TRACE_SYNC_INCOMPLETE_LOOKBACK_HOURS", 24), \
             patch.object(worker_mod, "TRACE_SYNC_SWEEP_BATCH_SIZE", 5), \
             patch.object(worker_mod, "async_session") as mock_session_factory:

            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)

            # 第一个查询：返回 1 个 synced_incomplete run
            run = MagicMock()
            run.id = "run-sweep-1"
            run.trace_sync_status = "synced_incomplete"
            run.started_at = datetime.utcnow()
            run.trace_id = "trace-sweep-1"

            # 第二个查询：active job 检查 → 返回有 job（跳过）
            # 第三个查询：找原 incomplete job → 不应到达（因为跳过了）
            call_count = [0]
            async def _mock_execute(stmt):
                call_count[0] += 1
                result = MagicMock()
                if call_count[0] == 1:
                    # runs 查询
                    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[run])))
                else:
                    # active job 查询 → 返回有 job
                    result.scalar_one_or_none = MagicMock(return_value="job-active-1")
                return result
            mock_session.execute = AsyncMock(side_effect=_mock_execute)
            mock_session_factory.return_value = mock_session

            count = await _sweep_incomplete_materialization()
            assert count == 0  # 被跳过，未 requeue
            # 不应有 commit
            mock_session.commit.assert_not_called()


# ── 5. 测试运行器 ──────────────────────────────────────────

async def run_all_tests():
    results = {"pass": 0, "fail": 0, "errors": []}

    async def _run(test_name, test_func):
        try:
            ret = test_func()
            # 如果返回 coroutine，await 它
            if asyncio.iscoroutine(ret):
                await ret
            results["pass"] += 1
            print(f"  PASS  {test_name}")
        except Exception as e:
            results["fail"] += 1
            results["errors"].append(f"{test_name}: {e}")
            print(f"  FAIL  {test_name}: {e}")

    # TestParseTs
    t = TestParseTs()
    await _run("test_valid_iso", t.test_valid_iso)
    await _run("test_valid_with_tz", t.test_valid_with_tz)
    await _run("test_none_input", t.test_none_input)
    await _run("test_empty_string", t.test_empty_string)
    await _run("test_garbage", t.test_garbage)
    await _run("test_naive_datetime", t.test_naive_datetime)

    # TestTimeWindowPadding
    t2 = TestTimeWindowPadding()
    await _run("test_padding_is_180", t2.test_padding_is_180)

    # TestFindTraceByTimeWindow
    t3 = TestFindTraceByTimeWindow()
    await _run("test_basic_match", t3.test_basic_match)
    await _run("test_no_traces", t3.test_no_traces)
    await _run("test_invalid_anchor", t3.test_invalid_anchor)
    await _run("test_claimed_trace_ids", t3.test_claimed_trace_ids)
    await _run("test_end_ts_extends_window", t3.test_end_ts_extends_window)
    await _run("test_list_traces_exception", t3.test_list_traces_exception)
    await _run("test_session_id_passed_to_list_traces", t3.test_session_id_passed_to_list_traces)

    # TestFindTraceBySession
    t4 = TestFindTraceBySession()
    await _run("test_no_session_id", t4.test_no_session_id)
    await _run("test_empty_session_id", t4.test_empty_session_id)
    await _run("test_exact_run_id_match", t4.test_exact_run_id_match)
    await _run("test_no_run_id_match_fallback_to_timestamp", t4.test_no_run_id_match_fallback_to_timestamp)
    await _run("test_list_traces_exception", t4.test_list_traces_exception)
    await _run("test_no_traces_returned", t4.test_no_traces_returned)

    # TestProcessTraceSyncJob
    t5 = TestProcessTraceSyncJob()
    await _run("test_deferred_too_young", t5.test_deferred_too_young)
    await _run("test_deferred_zero_delay", t5.test_deferred_zero_delay)
    await _run("test_empty_trace_id_skipped", t5.test_empty_trace_id_skipped)
    await _run("test_max_attempts_exceeded", t5.test_max_attempts_exceeded)
    await _run("test_langfuse_disabled", t5.test_langfuse_disabled)

    # TestBackwardCompatibility
    t6 = TestBackwardCompatibility()
    await _run("test_find_trace_no_new_params", t6.test_find_trace_no_new_params)
    await _run("test_process_trace_sync_job_no_claimed_trace_ids", t6.test_process_trace_sync_job_no_claimed_trace_ids)

    # TestCrossDrainTraceIdDedup
    t7 = TestCrossDrainTraceIdDedup()
    await _run("test_level1_skipped_when_trace_id_claimed_by_same_task_run", t7.test_level1_skipped_when_trace_id_claimed_by_same_task_run)

    # TestIncompleteMaterializationDeferral
    t8 = TestIncompleteMaterializationDeferral()
    await _run("test_defer_incomplete_first_two_reruns", t8.test_defer_incomplete_first_two_reruns)
    await _run("test_defer_incomplete_max_rerun_marks_synced_incomplete", t8.test_defer_incomplete_max_rerun_marks_synced_incomplete)
    await _run("test_defer_incomplete_writes_metadata", t8.test_defer_incomplete_writes_metadata)

    # TestSweepIncompleteMaterialization
    t9 = TestSweepIncompleteMaterialization()
    await _run("test_sweep_rate_limited", t9.test_sweep_rate_limited)
    await _run("test_sweep_skips_run_with_active_job", t9.test_sweep_skips_run_with_active_job)

    print(f"\n{'='*60}")
    print(f"Results: {results['pass']} passed, {results['fail']} failed")
    if results["errors"]:
        print("\nFailures:")
        for e in results["errors"]:
            print(f"  - {e}")
    print(f"{'='*60}")
    return results


if __name__ == "__main__":
    # 移除 pytest_mark_asyncio 装饰器的影响
    results = asyncio.run(run_all_tests())
    sys.exit(0 if results["fail"] == 0 else 1)
