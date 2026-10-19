"""稳定性分双子分重构测试。"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base, EvaluationResult, EvaluationTask
from services.quality_score_service import _compute_stability_score_v2


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_compute_runtime_pass_rates_online(mem_session: AsyncSession):
    """在线任务：从 EvaluationResult 聚合时延/token达标率。"""
    from services.quality_score_service import _compute_runtime_pass_rates

    task_id = "task_rt"
    mem_session.add(EvaluationTask(id=task_id, evaluation_mode="online"))
    # 5 题时延：3 达标（passed=True），2 超标
    for qid, lat, passed in [(0, 800, True), (1, 900, True), (2, 1200, False), (3, 1500, False), (4, 950, True)]:
        mem_session.add(EvaluationResult(
            task_id=task_id, question_id=qid, model="m",
            metric_name="inference_latency", eval_method="runtime",
            score=float(lat), avg_score=float(lat), threshold=1000, passed=passed,
        ))
    # 5 题 token：4 达标，1 超标
    for qid, tok, passed in [(0, 1000, True), (1, 1200, True), (2, 1800, False), (3, 1400, True), (4, 1300, True)]:
        mem_session.add(EvaluationResult(
            task_id=task_id, question_id=qid, model="m",
            metric_name="tokens_total", eval_method="runtime",
            score=float(tok), avg_score=float(tok), threshold=1500, passed=passed,
        ))
    await mem_session.flush()

    rates = await _compute_runtime_pass_rates(mem_session, task_id)
    assert rates is not None
    assert rates["latency"] == pytest.approx(0.6)  # 3/5
    assert rates["tokens"] == pytest.approx(0.8)  # 4/5


@pytest.mark.asyncio
async def test_compute_runtime_pass_rates_offline_returns_none(mem_session: AsyncSession):
    """离线任务 → None。"""
    from services.quality_score_service import _compute_runtime_pass_rates

    task_id = "task_offline"
    mem_session.add(EvaluationTask(id=task_id, evaluation_mode="offline"))
    rates = await _compute_runtime_pass_rates(mem_session, task_id)
    assert rates is None


@pytest.mark.asyncio
async def test_compute_runtime_pass_rates_no_runtime_metrics(mem_session: AsyncSession):
    """在线任务但无运行时指标行 → None。"""
    from services.quality_score_service import _compute_runtime_pass_rates

    task_id = "task_no_rt"
    mem_session.add(EvaluationTask(id=task_id, evaluation_mode="online"))
    rates = await _compute_runtime_pass_rates(mem_session, task_id)
    assert rates is None


@pytest.mark.asyncio
async def test_compute_runtime_pass_rates_only_latency(mem_session: AsyncSession):
    """只有时延无 token：返回只含 latency。"""
    from services.quality_score_service import _compute_runtime_pass_rates

    task_id = "task_lat_only"
    mem_session.add(EvaluationTask(id=task_id, evaluation_mode="online"))
    for qid, lat, passed in [(0, 800, True), (1, 1200, False)]:
        mem_session.add(EvaluationResult(
            task_id=task_id, question_id=qid, model="m",
            metric_name="inference_latency", eval_method="runtime",
            score=float(lat), avg_score=float(lat), threshold=1000, passed=passed,
        ))
    await mem_session.flush()

    rates = await _compute_runtime_pass_rates(mem_session, task_id)
    assert rates is not None
    assert rates["latency"] == pytest.approx(0.5)  # 1/2
    assert "tokens" not in rates


def test_stability_score_multi_round_with_runtime():

    correctness_stability = {
        "pass_rate": 0.92,
        "avg_std": 0.0458,
        "round_count": 10,
        "question_count": 6,
    }
    runtime_pass_rates = {"latency": 0.0, "tokens": 0.05}

    result = _compute_stability_score_v2(
        correctness_stability=correctness_stability,
        runtime_pass_rates=runtime_pass_rates,
        enable_stability=True,
    )
    # 质量稳定性 = 0.92×0.6 + (1-0.0458/0.30)×0.4 = 0.552 + 0.3461 = 0.8981
    # 性能稳定性 = 0.0×0.5 + 0.05×0.5 = 0.025
    # 稳定性分 = 0.8981×0.5 + 0.025×0.5 = 0.4616
    assert result["score"] == pytest.approx(0.4616, abs=0.01)
    assert result["is_valid"] is True
    assert result["breakdown"]["quality_stability"] == pytest.approx(0.8981, abs=0.01)
    assert result["breakdown"]["performance_stability"] == pytest.approx(0.025, abs=0.01)


def test_stability_score_single_round_degrades_to_performance():
    """单轮：质量子分=None，权重并入性能子分 → 稳定性分=性能子分。"""

    correctness_stability = {"pass_rate": None, "avg_std": None, "round_count": 1}
    runtime_pass_rates = {"latency": 0.8, "tokens": 0.9}

    result = _compute_stability_score_v2(
        correctness_stability=correctness_stability,
        runtime_pass_rates=runtime_pass_rates,
        enable_stability=True,
    )
    # 单轮：质量子分 None → 稳定性分 = 性能子分 = (0.8+0.9)/2 = 0.85
    assert result["score"] == pytest.approx(0.85)
    assert result["breakdown"]["quality_stability"] is None
    assert result["breakdown"]["performance_stability"] == pytest.approx(0.85)


def test_stability_score_no_runtime_degrades_to_quality():
    """无运行时指标（离线）：性能子分=None，权重并入质量子分 → 稳定性分=质量子分。"""

    correctness_stability = {
        "pass_rate": 0.92, "avg_std": 0.0458, "round_count": 10,
    }
    runtime_pass_rates = None  # 离线无运行时

    result = _compute_stability_score_v2(
        correctness_stability=correctness_stability,
        runtime_pass_rates=runtime_pass_rates,
        enable_stability=True,
    )
    # 无运行时：稳定性分 = 质量子分 = 0.8981
    assert result["score"] == pytest.approx(0.8981, abs=0.01)
    assert result["breakdown"]["performance_stability"] is None


def test_stability_score_both_none_returns_none():
    """二者都无：稳定性分=None。"""

    result = _compute_stability_score_v2(
        correctness_stability={"round_count": 1},
        runtime_pass_rates=None,
        enable_stability=True,
    )
    assert result["score"] is None
    assert result["is_valid"] is False


def test_stability_score_disabled_returns_none():
    """enable_stability=False：稳定性分=None（0.2 权重并入结果分）。"""

    result = _compute_stability_score_v2(
        correctness_stability={"pass_rate": 0.9, "avg_std": 0.05, "round_count": 10},
        runtime_pass_rates={"latency": 0.8, "tokens": 0.9},
        enable_stability=False,
    )
    assert result["score"] is None
    assert result["is_valid"] is False


def test_stability_score_partial_runtime_pass_rates():
    """运行时指标只有时延无 token：性能子分按时延单独算。"""

    correctness_stability = {"pass_rate": 0.9, "avg_std": 0.05, "round_count": 5}
    runtime_pass_rates = {"latency": 0.8}  # 只有时延

    result = _compute_stability_score_v2(
        correctness_stability=correctness_stability,
        runtime_pass_rates=runtime_pass_rates,
        enable_stability=True,
    )
    # 性能子分 = 0.8（只有时延，按可用分量归一化）
    assert result["breakdown"]["performance_stability"] == pytest.approx(0.8)


def test_stability_score_partial_quality_components():
    """质量稳定性缺 std（仅 pass_rate）：按 pass_rate 单独算。"""

    correctness_stability = {"pass_rate": 0.9, "avg_std": None, "round_count": 5}
    runtime_pass_rates = {"latency": 0.8, "tokens": 0.9}

    result = _compute_stability_score_v2(
        correctness_stability=correctness_stability,
        runtime_pass_rates=runtime_pass_rates,
        enable_stability=True,
    )
    # 质量子分 = 0.9（只有 pass_rate）
    assert result["breakdown"]["quality_stability"] == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_compute_task_quality_score_with_performance_stability(mem_session: AsyncSession):
    """端到端：在线多轮任务，稳定性分含性能子分。"""
    from database import EvaluationTask, EvaluationResult, AgentRun, CheckpointResult, ProcessCheckpointTemplate
    from services.quality_score_service import compute_task_quality_score
    from datetime import datetime

    task_id = "task_e2e"
    mem_session.add(EvaluationTask(
        id=task_id, evaluation_mode="online", inference_rounds=3,
        enable_process_consistency=True, enable_stability=True,
    ))
    # 2 题 × 3 轮 correctness（多轮质量指标）
    for qid in [0, 1]:
        for rid in range(3):
            mem_session.add(EvaluationResult(
                task_id=task_id, question_id=qid, model="m",
                metric_name="correctness", agent_run_id=f"r{qid}_{rid}",
                score=0.85, avg_score=0.85, threshold=0.5, passed=True,
            ))
    # 运行时指标：2 题时延全达标，2 题 token 一半达标
    for qid, lat, passed in [(0, 800, True), (1, 900, True)]:
        mem_session.add(EvaluationResult(
            task_id=task_id, question_id=qid, model="m",
            metric_name="inference_latency", eval_method="runtime",
            score=float(lat), avg_score=float(lat), threshold=1000, passed=passed,
        ))
    for qid, tok, passed in [(0, 1000, True), (1, 1800, False)]:
        mem_session.add(EvaluationResult(
            task_id=task_id, question_id=qid, model="m",
            metric_name="tokens_total", eval_method="runtime",
            score=float(tok), avg_score=float(tok), threshold=1500, passed=passed,
        ))
    # 过程分需要的最小数据（避免 process_score=None）
    mem_session.add(ProcessCheckpointTemplate(
        id="tpl1", logical_agent_id="logical-a", name="必达点1", order=1, score_weight=0.1, required=True, enabled=True,
        match_rules='{"mode":"any","rules":[]}', match_mode="rule",
    ))
    for qid in [0, 1]:
        for rid in range(3):
            mem_session.add(AgentRun(
                id=f"r{qid}_{rid}", task_id=task_id, question_id=qid, agent_id="ag1",
                run_index=rid, started_at=datetime.utcnow(), agent_snapshot="{}",
            ))
            mem_session.add(CheckpointResult(
                id=f"cp_{qid}_{rid}", agent_run_id=f"r{qid}_{rid}",
                checkpoint_id="tpl1", passed=True,
            ))
    await mem_session.flush()

    score = await compute_task_quality_score(mem_session, task_id)
    # 稳定性分应非 None（多轮 + 运行时齐全）
    assert score["stability_score"] is not None
    assert score["stability_weight"] == pytest.approx(0.2)
    sb = score["stability_breakdown"]
    assert sb["quality_stability"] is not None
    assert sb["performance_stability"] is not None
    # 性能子分 = 时延1.0×0.5 + token0.5×0.5 = 0.75
    assert sb["performance_stability"] == pytest.approx(0.75, abs=0.01)


@pytest.mark.asyncio
async def test_compute_task_quality_score_offline_no_performance(mem_session: AsyncSession):
    """离线任务：性能子分 None，稳定性分=质量子分。"""
    from database import EvaluationTask, EvaluationResult
    from services.quality_score_service import compute_task_quality_score

    task_id = "task_offline_e2e"
    mem_session.add(EvaluationTask(
        id=task_id, evaluation_mode="offline", inference_rounds=3,
        enable_process_consistency=False, enable_stability=True,
    ))
    # 多轮质量指标（无过程分、无运行时）
    for qid in [0, 1]:
        for rid in range(3):
            mem_session.add(EvaluationResult(
                task_id=task_id, question_id=qid, model="m",
                metric_name="correctness", agent_run_id=f"o{qid}_{rid}",
                score=0.85, avg_score=0.85, threshold=0.5, passed=True,
            ))
    await mem_session.flush()

    score = await compute_task_quality_score(mem_session, task_id)
    # 离线：性能子分 None，稳定性分=质量子分
    sb = score["stability_breakdown"]
    assert sb["performance_stability"] is None
    assert sb["quality_stability"] is not None
    assert score["stability_score"] == pytest.approx(sb["quality_stability"])


@pytest.mark.asyncio
async def test_stability_disabled_weight_merges_to_result(mem_session: AsyncSession):
    """enable_stability=False：稳定性分 None，0.2 并入结果分。"""
    from database import EvaluationTask, EvaluationResult
    from services.quality_score_service import compute_task_quality_score

    task_id = "task_stab_off"
    mem_session.add(EvaluationTask(
        id=task_id, evaluation_mode="online", inference_rounds=3,
        enable_process_consistency=False, enable_stability=False,
    ))
    for qid in [0, 1]:
        for rid in range(3):
            mem_session.add(EvaluationResult(
                task_id=task_id, question_id=qid, model="m",
                metric_name="correctness", agent_run_id=f"s{qid}_{rid}",
                score=0.9, avg_score=0.9, threshold=0.5, passed=True,
            ))
    await mem_session.flush()

    score = await compute_task_quality_score(mem_session, task_id)
    assert score["stability_score"] is None
    assert score["stability_weight"] == pytest.approx(0.0)
    # 0.2 并入结果分：result_weight = 0.6 + 0.2 = 0.8（过程分也禁用，再加 0.2 = 1.0）
    assert score["result_weight"] == pytest.approx(1.0)