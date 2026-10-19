"""任务级评测四维结论读模型单测（P0-a）。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import (
    AgentRun,
    Base,
    CheckpointResult,
    EvaluationResult,
    EvaluationTask,
    ProcessCheckpointTemplate,
    RunSpanNode,
    ScenarioEvalStep,
    ScenarioEvalTemplate,
    ScenarioStepResult,
)
from services.eval_context import build_eval_context
from services.evaluation_mode import EVALUATION_MODE_OFFLINE, EVALUATION_MODE_ONLINE
from services.task_dimension_verdict_service import build_task_dimension_verdicts
from services.quality_score_service import compute_task_quality_score

pytestmark = pytest.mark.p3


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed_offline_quality_task(session: AsyncSession):
    session.add(
        EvaluationTask(
            id="t_off_verdict",
            dataset_id="ds1",
            models='["glm"]',
            status="completed",
            evaluation_mode=EVALUATION_MODE_OFFLINE,
            agent_id=None,
            created_at=datetime.utcnow(),
        )
    )
    session.add(
        EvaluationResult(
            task_id="t_off_verdict",
            question_id=0,
            model="glm",
            metric_name="faithfulness",
            score=0.9,
            threshold=0.8,
            passed=True,
            duration_ms=1,
            avg_score=0.9,
        )
    )
    await session.commit()


async def _seed_process_fail_task(session: AsyncSession):
    session.add(
        EvaluationTask(
            id="t_proc_fail",
            dataset_id="ds1",
            models='["glm"]',
            status="completed",
            agent_id="ag1",
            evaluation_mode=EVALUATION_MODE_ONLINE,
            inference_rounds=2,
            created_at=datetime.utcnow(),
        )
    )
    session.add(
        ProcessCheckpointTemplate(
            id="tpl_v_plan",
            logical_agent_id="logical-a",
            name="规划",
            order=1,
            required=True,
            enforce_order=True,
            match_rules='{"mode":"any","rules":[]}',
            enabled=True,
        )
    )
    for idx, run_id in enumerate(["run_vf_r0", "run_vf_r1"]):
        session.add(
            AgentRun(
                id=run_id,
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t_proc_fail", dataset_id="d1", question_id=0, run_index=idx
                ),
                task_id="t_proc_fail",
                dataset_id="d1",
                question_id=0,
                run_index=idx,
                agent_id="ag1",
                agent_name="Agent A",
                logical_agent_id="logical-a",
                status="success",
                started_at=datetime.utcnow(),
                trace_sync_status="synced",
                agent_snapshot="{}",
            )
        )
    session.add_all(
        [
            CheckpointResult(
                id="cpr_vf_r0",
                agent_run_id="run_vf_r0",
                checkpoint_id="tpl_v_plan",
                passed=True,
                evidence={"hit": True, "order_ok": True},
            ),
            CheckpointResult(
                id="cpr_vf_r1",
                agent_run_id="run_vf_r1",
                checkpoint_id="tpl_v_plan",
                passed=False,
                evidence={"hit": True, "order_ok": False},
            ),
        ]
    )
    await session.commit()


async def _seed_online_minimal_task(session: AsyncSession):
    session.add(
        EvaluationTask(
            id="t_online_min",
            dataset_id="ds1",
            models='["glm"]',
            status="completed",
            agent_id="ag1",
            evaluation_mode=EVALUATION_MODE_ONLINE,
            inference_rounds=1,
            created_at=datetime.utcnow(),
        )
    )
    session.add(
        AgentRun(
            id="run_online_min",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t_online_min", dataset_id="d1", question_id=0, run_index=0
            ),
            task_id="t_online_min",
            dataset_id="d1",
            question_id=0,
            run_index=0,
            agent_id="ag1",
            status="success",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
        )
    )
    session.add(
        EvaluationResult(
            task_id="t_online_min",
            question_id=0,
            model="glm",
            metric_name="inference_latency",
            score=1.0,
            threshold=5000,
            passed=True,
            duration_ms=1,
            avg_score=1.0,
            inference_duration_ms=1200,
            eval_method="runtime",
        )
    )
    await session.commit()


async def _seed_scenario_task(session: AsyncSession):
    session.add(
        EvaluationTask(
            id="t_scenario",
            dataset_id="ds1",
            models='["glm"]',
            status="completed",
            agent_id="ag1",
            evaluation_mode=EVALUATION_MODE_ONLINE,
            inference_rounds=1,
            created_at=datetime.utcnow(),
        )
    )
    session.add(
        ScenarioEvalTemplate(
            id="tpl_sc",
            scenario_key="demo_sc",
            name="Demo",
            ground_truth_ref="gt.md",
            created_at=datetime.utcnow(),
        )
    )
    session.add(
        ScenarioEvalStep(
            id="step_sc_s1",
            template_id="tpl_sc",
            step_key="S1",
            name="步骤一",
            step_order=1,
            weight=1.0,
            required=True,
            enabled=True,
        )
    )
    session.add(
        AgentRun(
            id="run_scenario",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t_scenario", dataset_id="d1", question_id=0, run_index=0
            ),
            task_id="t_scenario",
            dataset_id="d1",
            question_id=0,
            run_index=0,
            agent_id="ag1",
            status="success",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
        )
    )
    session.add(
        ScenarioStepResult(
            id="ssr_sc_1",
            agent_run_id="run_scenario",
            step_id="step_sc_s1",
            score=0.85,
            passed=True,
            evidence={"matched_keywords": ["ok"]},
        )
    )
    await session.commit()


async def _seed_multi_round_structural_task(session: AsyncSession):
    session.add(
        EvaluationTask(
            id="t_struct",
            dataset_id="ds1",
            models='["glm"]',
            status="completed",
            agent_id="ag1",
            evaluation_mode=EVALUATION_MODE_ONLINE,
            inference_rounds=2,
            created_at=datetime.utcnow(),
        )
    )
    for idx, run_id in enumerate(["run_st_r0", "run_st_r1"]):
        session.add(
            AgentRun(
                id=run_id,
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t_struct", dataset_id="d1", question_id=0, run_index=idx
                ),
                task_id="t_struct",
                dataset_id="d1",
                question_id=0,
                run_index=idx,
                agent_id="ag1",
                status="success",
                started_at=datetime.utcnow(),
                agent_snapshot="{}",
            )
        )
        session.add(
            RunSpanNode(
                id=f"span_st_{idx}_a",
                agent_run_id=run_id,
                observation_id=f"obs_st_{idx}_a",
                step_index=0,
                sibling_order=0,
                node_type="generation",
                name="main",
                attributes={},
            )
        )
        if idx == 1:
            session.add(
                RunSpanNode(
                    id=f"span_st_{idx}_b",
                    agent_run_id=run_id,
                    observation_id=f"obs_st_{idx}_b",
                    step_index=1,
                    sibling_order=0,
                    node_type="tool",
                    name="search",
                    attributes={"agent.tool.name": "search"},
                )
            )
    await session.commit()


@pytest.mark.asyncio
async def test_offline_only_quality_non_na(mem_session):
    await _seed_offline_quality_task(mem_session)
    payload = await build_task_dimension_verdicts(mem_session, "t_off_verdict")

    assert payload is not None
    assert payload["evaluation_mode"] == "offline"
    assert payload["quality"]["status"] in ("pass", "warn", "fail")
    assert payload["quality"]["headline"]
    assert payload["process_consistency"]["status"] == "na"
    assert payload["scenario_delivery"]["status"] == "na"
    assert payload["runtime_stability"]["structural"]["status"] == "na"
    assert payload["runtime_stability"]["performance"]["status"] == "na"


@pytest.mark.asyncio
async def test_offline_with_process_eval_scenario_non_na(mem_session):
    """离线任务完成过程评估后，场景子步骤 verdict 应返回实际数据而非 NA。"""
    session = mem_session
    await _seed_offline_quality_task(session)

    task = await session.get(EvaluationTask, "t_off_verdict")
    task.process_eval_completed = True
    await session.commit()

    payload = await build_task_dimension_verdicts(session, "t_off_verdict")

    assert payload is not None
    assert payload["evaluation_mode"] == "offline"
    assert payload["process_consistency"]["status"] == "na"
    assert payload["scenario_delivery"]["status"] == "na"
    assert payload["scenario_delivery"]["headline"] == "未配置场景子步骤或未产出打分"
    assert payload["runtime_stability"]["structural"]["status"] == "na"
    assert payload["runtime_stability"]["performance"]["status"] == "na"


@pytest.mark.asyncio
async def test_process_consistency_fail_on_red(mem_session):
    await _seed_process_fail_task(mem_session)
    payload = await build_task_dimension_verdicts(mem_session, "t_proc_fail")

    assert payload["process_consistency"]["status"] == "fail"
    assert "必达未过" in payload["process_consistency"]["headline"]
    assert payload["process_consistency"]["metrics"]["red"] == 1

    quality_score = await compute_task_quality_score(mem_session, "t_proc_fail")
    assert payload["process_consistency"]["score"] == quality_score["process_score"]


@pytest.mark.asyncio
async def test_process_consistency_score_matches_quality_process_score(mem_session):
    await _seed_process_fail_task(mem_session)
    payload = await build_task_dimension_verdicts(mem_session, "t_proc_fail")
    quality_score = await compute_task_quality_score(mem_session, "t_proc_fail")
    assert payload["process_consistency"]["score"] == quality_score["process_score"]


@pytest.mark.asyncio
async def test_quality_verdict_not_failed_by_cross_dimension_gate(mem_session):
    """quality 维度 status 不得因 process_consistency gate 翻转为 fail。

    场景：质量指标全部通过（result_score 高、pass_rate 100%），但过程一致性 red>0
    触发 process_consistency_fail 与 required_checkpoint_pass_rate_low gate。
    这些跨维度 gate 只应封顶 total_score，不应翻转 quality 维度的 status。
    """
    await _seed_process_fail_task(mem_session)
    # 补齐全部通过的质量指标，确保 quality 自身无 fail 信号
    session = mem_session
    for run_id in ("run_vf_r0", "run_vf_r1"):
        for metric, score in (
            ("correctness", 0.9),
            ("faithfulness", 0.95),
            ("answer_relevancy", 0.92),
        ):
            session.add(
                EvaluationResult(
                    task_id="t_proc_fail",
                    question_id=0,
                    model="glm",
                    metric_name=metric,
                    score=score,
                    threshold=0.8,
                    passed=True,
                    duration_ms=1,
                    avg_score=score,
                    agent_run_id=run_id,
                )
            )
    await session.commit()

    payload = await build_task_dimension_verdicts(mem_session, "t_proc_fail")

    # quality 维度应 pass（自身指标全过），不得被跨维度 gate 拖成 fail
    assert payload["quality"]["status"] == "pass", (
        f"quality.status 应为 pass，实际 {payload['quality']['status']}；"
        f"headline={payload['quality']['headline']}"
    )
    # quality.metrics.gates 不得含跨维度 gate
    gate_types = [g["type"] for g in payload["quality"]["metrics"]["gates"]]
    assert "process_consistency_fail" not in gate_types, (
        f"跨维度 gate 不应出现在 quality.metrics.gates：{gate_types}"
    )
    assert "required_checkpoint_pass_rate_low" not in gate_types, (
        f"跨维度 gate 不应出现在 quality.metrics.gates：{gate_types}"
    )
    # total_score 仍应被封顶（跨维度 gate 封 total 是正确的）
    quality_score = await compute_task_quality_score(mem_session, "t_proc_fail")
    assert quality_score["total_score"] is not None
    assert quality_score["total_score"] <= 0.75, (
        f"total_score 应被 process gate 封顶至 ≤0.75，实际 {quality_score['total_score']}"
    )
    # raw gates 仍含跨维度 gate（供前端分数 tooltip 展示）
    raw_gate_types = [g["type"] for g in quality_score["gates"]]
    assert "process_consistency_fail" in raw_gate_types
    # 过程一致性维度自身仍是 fail
    assert payload["process_consistency"]["status"] == "fail"


@pytest.mark.asyncio
async def test_scenario_na_without_steps(mem_session):
    await _seed_online_minimal_task(mem_session)
    payload = await build_task_dimension_verdicts(mem_session, "t_online_min")

    assert payload["scenario_delivery"]["status"] == "na"
    assert payload["runtime_stability"]["structural"]["status"] == "na"


@pytest.mark.asyncio
async def test_scenario_pass_with_step_results(mem_session):
    await _seed_scenario_task(mem_session)
    payload = await build_task_dimension_verdicts(mem_session, "t_scenario")

    assert payload["scenario_delivery"]["status"] in ("pass", "warn")
    assert payload["scenario_delivery"]["score"] is not None
    assert "场景综合" in payload["scenario_delivery"]["headline"]


@pytest.mark.asyncio
async def test_runtime_structural_always_na(mem_session):
    """单轮推理任务的稳定性维度整体返回 na，headline 说明原因。"""
    await _seed_online_minimal_task(mem_session)
    payload = await build_task_dimension_verdicts(mem_session, "t_online_min")

    assert payload["runtime_stability"]["structural"]["status"] == "na"
    assert payload["runtime_stability"]["performance"]["status"] == "na"
    assert "单轮" in payload["runtime_stability"]["structural"]["headline"]


@pytest.mark.asyncio
async def test_runtime_multi_round_structural_still_na(mem_session):
    await _seed_multi_round_structural_task(mem_session)
    payload = await build_task_dimension_verdicts(mem_session, "t_struct")

    assert payload["runtime_stability"]["structural"]["status"] == "na"
    assert payload["runtime_stability"]["structural"]["headline"] == "步骤结构评测已下线"


@pytest.mark.asyncio
async def test_all_headlines_non_empty(mem_session):
    await _seed_process_fail_task(mem_session)
    payload = await build_task_dimension_verdicts(mem_session, "t_proc_fail")

    for key in ("quality", "process_consistency", "scenario_delivery", "runtime_stability"):
        assert payload[key]["headline"]
    assert payload["runtime_stability"]["structural"]["headline"]
    assert payload["runtime_stability"]["performance"]["headline"]


@pytest.mark.asyncio
async def test_missing_task_returns_none(mem_session):
    assert await build_task_dimension_verdicts(mem_session, "missing") is None


@pytest.mark.asyncio
async def test_online_process_consistency_disabled_status(mem_session):
    """enable_process_consistency=False → 过程一致性结论 status=disabled。"""
    await _seed_online_minimal_task(mem_session)
    task = await mem_session.get(EvaluationTask, "t_online_min")
    task.enable_process_consistency = False
    await mem_session.commit()

    payload = await build_task_dimension_verdicts(mem_session, "t_online_min")
    assert payload is not None
    assert payload["process_consistency"]["status"] == "disabled"
    assert "未启用" in payload["process_consistency"]["headline"]


@pytest.mark.asyncio
async def test_online_stability_disabled_status(mem_session):
    """enable_stability=False → 运行稳定性结论 status=disabled，不影响其他维度。"""
    await _seed_online_minimal_task(mem_session)
    task = await mem_session.get(EvaluationTask, "t_online_min")
    task.enable_stability = False
    await mem_session.commit()

    payload = await build_task_dimension_verdicts(mem_session, "t_online_min")
    assert payload is not None
    assert payload["runtime_stability"]["status"] == "disabled"
    assert payload["runtime_stability"]["performance"]["status"] == "disabled"
    assert "未启用" in payload["runtime_stability"]["headline"]
    # 质量维度不受稳定性开关影响（仍正常计算）
    assert payload["quality"]["headline"]


@pytest.mark.asyncio
async def test_offline_stability_disabled_status(mem_session):
    """离线任务 enable_stability=False → 稳定性结论 status=disabled。"""
    await _seed_offline_quality_task(mem_session)
    task = await mem_session.get(EvaluationTask, "t_off_verdict")
    task.enable_stability = False
    await mem_session.commit()

    payload = await build_task_dimension_verdicts(mem_session, "t_off_verdict")
    assert payload is not None
    assert payload["runtime_stability"]["status"] == "disabled"
    assert "未启用" in payload["runtime_stability"]["headline"]


@pytest.mark.asyncio
async def test_scenario_disabled_status_online(mem_session):
    """auto_scenario_score=False → 场景交付结论 status=disabled。"""
    await _seed_scenario_task(mem_session)
    task = await mem_session.get(EvaluationTask, "t_scenario")
    task.auto_scenario_score = False
    await mem_session.commit()

    payload = await build_task_dimension_verdicts(mem_session, "t_scenario")
    assert payload is not None
    assert payload["scenario_delivery"]["status"] == "disabled"
    assert "未启用" in payload["scenario_delivery"]["headline"]


@pytest.mark.asyncio
async def test_online_dimension_flags_default_true(mem_session):
    """不显式设置开关时，三维度均默认启用（行为与改动前一致）。"""
    await _seed_online_minimal_task(mem_session)
    payload = await build_task_dimension_verdicts(mem_session, "t_online_min")
    assert payload is not None
    # 过程一致性应正常计算（非 NA 因开关），稳定性也应非纯 NA
    assert payload["process_consistency"]["status"] != "na" or payload["process_consistency"]["headline"]


async def _seed_quality_gate_task(session: AsyncSession):
    """correctness 2 题中 1 题失败(pass_rate=50%) → 触发 quality_metric_pass_rate_low gate。"""
    session.add(
        EvaluationTask(
            id="t_qual_gate",
            dataset_id="ds1",
            models='["glm"]',
            status="completed",
            agent_id="ag1",
            evaluation_mode=EVALUATION_MODE_ONLINE,
            inference_rounds=1,
            created_at=datetime.utcnow(),
        )
    )
    session.add(
        AgentRun(
            id="run_qual_gate",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t_qual_gate", dataset_id="d1", question_id=0, run_index=0
            ),
            task_id="t_qual_gate",
            dataset_id="d1",
            question_id=0,
            run_index=0,
            agent_id="ag1",
            status="success",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
        )
    )
    # correctness: q0 通过, q1 失败 → pass_rate=50%
    session.add_all([
        EvaluationResult(
            task_id="t_qual_gate", question_id=0, model="glm",
            metric_name="correctness", score=0.9, threshold=0.8,
            passed=True, duration_ms=1, avg_score=0.9,
        ),
        EvaluationResult(
            task_id="t_qual_gate", question_id=1, model="glm",
            metric_name="correctness", score=0.4, threshold=0.8,
            passed=False, duration_ms=1, avg_score=0.4,
        ),
        EvaluationResult(
            task_id="t_qual_gate", question_id=0, model="glm",
            metric_name="faithfulness", score=0.9, threshold=0.8,
            passed=True, duration_ms=1, avg_score=0.9,
        ),
        EvaluationResult(
            task_id="t_qual_gate", question_id=1, model="glm",
            metric_name="faithfulness", score=0.9, threshold=0.8,
            passed=True, duration_ms=1, avg_score=0.9,
        ),
    ])
    await session.commit()


@pytest.mark.asyncio
async def test_quality_metric_pass_rate_gate_passes_through_to_verdict(mem_session):
    """quality_metric_pass_rate_low gate 必须透传到 dimension_verdicts.quality.metrics.gates。"""
    await _seed_quality_gate_task(mem_session)
    payload = await build_task_dimension_verdicts(mem_session, "t_qual_gate")

    quality_metrics = payload["quality"]["metrics"]
    gate_types = [g["type"] for g in quality_metrics["gates"]]
    assert "quality_metric_pass_rate_low" in gate_types, (
        f"expected quality_metric_pass_rate_low in verdict gates, got {gate_types}"
    )
    # 透传的 gate 必须带 cap 和 reason
    qg = next(g for g in quality_metrics["gates"] if g["type"] == "quality_metric_pass_rate_low")
    assert qg["cap"] == 0.50
    assert qg["reason"]  # 非空


@pytest.mark.asyncio
async def test_safety_pass_rate_gate_passes_through_to_verdict(mem_session):
    """safety_pass_rate_low gate 也必须透传（验证不止 low_result_score 一种）。"""
    # 复用 quality_gate_task，再补一条 pii_leakage 失败
    await _seed_quality_gate_task(mem_session)
    session = mem_session
    session.add_all([
        EvaluationResult(
            task_id="t_qual_gate", question_id=0, model="glm",
            metric_name="pii_leakage", score=1.0, threshold=0.8,
            passed=True, duration_ms=1, avg_score=1.0,
        ),
        EvaluationResult(
            task_id="t_qual_gate", question_id=1, model="glm",
            metric_name="pii_leakage", score=0.4, threshold=0.8,
            passed=False, duration_ms=1, avg_score=0.4,
        ),
    ])
    await session.commit()

    payload = await build_task_dimension_verdicts(mem_session, "t_qual_gate")
    gate_types = [g["type"] for g in payload["quality"]["metrics"]["gates"]]
    assert "safety_pass_rate_low" in gate_types, (
        f"expected safety_pass_rate_low in verdict gates, got {gate_types}"
    )


@pytest.mark.asyncio
async def test_online_single_round_stability_na(mem_session):
    """inference_rounds=1 时稳定性整体返回 na，不计算性能 verdict。"""
    await _seed_online_minimal_task(mem_session)
    payload = await build_task_dimension_verdicts(mem_session, "t_online_min")

    assert payload is not None
    assert payload["inference_rounds"] == 1
    rs = payload["runtime_stability"]
    assert rs["status"] == "na"
    assert rs["performance"]["status"] == "na"
    assert "单轮" in rs["headline"]
    # quality_stability 也应是 na
    assert rs["quality_stability"]["status"] == "na"

