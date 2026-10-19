"""质量总分 v1 聚合规则单测。"""
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
    ScenarioEvalStep,
    ScenarioEvalTemplate,
    ScenarioStepResult,
)
from services.eval_context import build_eval_context
from services.evaluation_mode import EVALUATION_MODE_ONLINE
from services.quality_score_service import compute_task_quality_score
from services.task_list_service import serialize_tasks_for_list

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


async def _seed_quality_case(session: AsyncSession, *, task_id: str = "task_q1"):
    session.add(
        EvaluationTask(
            id=task_id,
            dataset_id="ds1",
            models='["glm"]',
            status="completed",
            evaluation_mode="online",
            agent_id="ag1",
        )
    )
    session.add_all(
        [
            EvaluationResult(
                task_id=task_id,
                question_id=0,
                model="glm",
                metric_name="correctness",
                score=0.8,
                avg_score=0.8,
                threshold=0.5,
                passed=True,
            ),
            EvaluationResult(
                task_id=task_id,
                question_id=0,
                model="glm",
                metric_name="faithfulness",
                score=0.6,
                avg_score=0.6,
                threshold=0.5,
                passed=True,
            ),
            EvaluationResult(
                task_id=task_id,
                question_id=0,
                model="glm",
                metric_name="tokens_total",
                score=900,
                avg_score=900,
                threshold=1200,
                passed=True,
                eval_method="runtime",
            ),
        ]
    )
    session.add(
        AgentRun(
            id="run_q1",
            task_id=task_id,
            dataset_id="ds1",
            question_id=0,
            agent_id="ag1",
            status="completed",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
        )
    )
    session.add(
        ProcessCheckpointTemplate(
            id="tpl_cp_plan",
            logical_agent_id="logical-a",
            name="规划",
            order=1,
            required=True,
            enforce_order=True,
            match_rules='{"mode":"any","rules":[]}',
            enabled=True,
        )
    )
    session.add(
        CheckpointResult(
            id="cpr_q1",
            agent_run_id="run_q1",
            checkpoint_id="tpl_cp_plan",
            passed=True,
            evidence={"hit": True, "order_ok": True},
        )
    )
    session.add(ScenarioEvalTemplate(id="tpl_q1", scenario_key="case_q1", name="场景"))
    session.add_all(
        [
            ScenarioEvalStep(
                id="step_q1_a",
                template_id="tpl_q1",
                step_key="S1",
                name="必做步骤",
                step_order=1,
                weight=0.7,
                required=True,
            ),
            ScenarioEvalStep(
                id="step_q1_b",
                template_id="tpl_q1",
                step_key="S2",
                name="辅助步骤",
                step_order=2,
                weight=0.3,
                required=False,
            ),
        ]
    )
    session.add_all(
        [
            ScenarioStepResult(
                id="ssr_q1_a",
                agent_run_id="run_q1",
                step_id="step_q1_a",
                score=0.2,
                passed=False,
                evidence={"observation_id": "obs1"},
            ),
            ScenarioStepResult(
                id="ssr_q1_b",
                agent_run_id="run_q1",
                step_id="step_q1_b",
                score=0.1,
                passed=False,
                evidence={},
            ),
        ]
    )
    await session.flush()


@pytest.mark.asyncio
async def test_compute_task_quality_score_v1(mem_session):
    await _seed_quality_case(mem_session)

    score = await compute_task_quality_score(mem_session, "task_q1")

    assert score["result_score"] == pytest.approx(0.7556)
    assert score["process_score"] == pytest.approx(1.0)
    # 单轮 stability disabled 后 total = 0.8×0.7556 + 0.2×1.0 = 0.8044
    assert score["total_score"] == pytest.approx(0.8044)
    assert score["required_pass_rate"] == pytest.approx(1.0)
    assert score["gates"] == []
    assert score["stability_score"] is None
    assert score["stability_weight"] == pytest.approx(0.0)
    assert score["result_weight"] == pytest.approx(0.8)
    assert score["process_weight"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_quality_score_without_checkpoints_uses_result_only(mem_session):
    await _seed_quality_case(mem_session, task_id="task_no_cp")
    await mem_session.execute(
        CheckpointResult.__table__.delete().where(CheckpointResult.id == "cpr_q1")
    )
    await mem_session.execute(
        ProcessCheckpointTemplate.__table__.delete().where(
            ProcessCheckpointTemplate.id == "tpl_cp_plan"
        )
    )
    await mem_session.flush()

    score = await compute_task_quality_score(mem_session, "task_no_cp")

    assert score["result_score"] == pytest.approx(0.7556)
    assert score["process_score"] is None
    assert score["process_weight"] == pytest.approx(0.0)
    # 无 CheckpointResult → 过程分不生效，权重并入结果分（required_total=0，不触发 process_consistency_fail gate）
    gate_types = [g["type"] for g in score["gates"]]
    assert "process_consistency_fail" not in gate_types
    assert score["total_score"] == pytest.approx(0.7556)  # total = result_score
    assert score["stability_score"] is None
    assert score["stability_weight"] == pytest.approx(0.0)
    assert score["result_weight"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_quality_score_gate_caps_low_result(mem_session):
    await _seed_quality_case(mem_session, task_id="task_gate")
    rows = await mem_session.execute(
        EvaluationResult.__table__.update()
        .where(EvaluationResult.task_id == "task_gate")
        .where(EvaluationResult.metric_name.in_(["correctness", "faithfulness"]))
        .values(score=0.4, avg_score=0.4)
    )
    assert rows.rowcount == 2
    await mem_session.flush()

    score = await compute_task_quality_score(mem_session, "task_gate")

    # 单轮 stability disabled 后 raw_total = 0.8×0.4 + 0.2×1.0 = 0.52
    # result_score = (0.4×0.7 + 0.4×0.2) / (0.7+0.2) = 0.4（correctness/faithfulness各0.4）
    # 结果分<0.5 触发 low_result_score gate cap=0.55，但 min(0.52, 0.55)=0.52
    assert score["raw_total_score"] == pytest.approx(0.52)
    assert score["total_score"] == pytest.approx(0.52)
    assert score["gates"][0]["type"] == "low_result_score"


@pytest.mark.asyncio
async def test_task_list_includes_quality_score(mem_session):
    await _seed_quality_case(mem_session, task_id="task_list_quality")
    task = await mem_session.get(EvaluationTask, "task_list_quality")

    rows = await serialize_tasks_for_list(mem_session, [task])

    # 单轮 stability disabled 后 total=0.8044
    assert rows[0]["quality_score"]["total_score"] == pytest.approx(0.8044)
    assert rows[0]["total_score"] == pytest.approx(0.8044)
    assert rows[0]["result_score"] == pytest.approx(0.7556)
    assert rows[0]["process_score"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_quality_score_process_disabled_excludes_process(mem_session):
    """enable_process_consistency=False → 即使有 CheckpointResult，process_score 也为 None、
    process_weight=0、total_score 退化为纯 result_score。"""
    await _seed_quality_case(mem_session, task_id="task_pc_off")
    task = await mem_session.get(EvaluationTask, "task_pc_off")
    task.enable_process_consistency = False
    await mem_session.flush()

    score = await compute_task_quality_score(mem_session, "task_pc_off")

    assert score["result_score"] == pytest.approx(0.7556)
    assert score["process_score"] is None
    assert score["process_weight"] == pytest.approx(0.0)
    # 单轮 stability disabled 后 total = 1.0×0.7556 = 0.7556（全部权重归 result）
    assert score["total_score"] == pytest.approx(0.7556)
    assert score["stability_score"] is None
    assert score["stability_weight"] == pytest.approx(0.0)
    assert score["result_weight"] == pytest.approx(1.0)


async def _seed_multi_round_case(session: AsyncSession, *, task_id: str = "task_multi"):
    """构造 2轮×2题 多轮场景：correctness 有波动，必达点全通过。

    Q0: correctness=[0.9, 0.7]  Q1: correctness=[0.8, 0.6]（均 passed=True）
    → result_score=0.75, process_score=1.0, pass_rate=1.0, avg_std=0.1, chain_pass_rate=1.0
    """
    session.add(
        EvaluationTask(
            id=task_id,
            dataset_id="ds1",
            models='["glm"]',
            status="completed",
            evaluation_mode="online",
            agent_id="ag1",
        )
    )
    runs = [
        AgentRun(id=f"run_{task_id}_q0_r0", task_id=task_id, dataset_id="ds1", question_id=0,
                 agent_id="ag1", status="completed", started_at=datetime.utcnow(), agent_snapshot="{}"),
        AgentRun(id=f"run_{task_id}_q0_r1", task_id=task_id, dataset_id="ds1", question_id=0,
                 agent_id="ag1", status="completed", started_at=datetime.utcnow(), agent_snapshot="{}"),
        AgentRun(id=f"run_{task_id}_q1_r0", task_id=task_id, dataset_id="ds1", question_id=1,
                 agent_id="ag1", status="completed", started_at=datetime.utcnow(), agent_snapshot="{}"),
        AgentRun(id=f"run_{task_id}_q1_r1", task_id=task_id, dataset_id="ds1", question_id=1,
                 agent_id="ag1", status="completed", started_at=datetime.utcnow(), agent_snapshot="{}"),
    ]
    session.add_all(runs)
    session.add_all([
        EvaluationResult(task_id=task_id, question_id=0, agent_run_id=f"run_{task_id}_q0_r0",
                        model="glm", metric_name="correctness", score=0.9, avg_score=0.9, threshold=0.5, passed=True),
        EvaluationResult(task_id=task_id, question_id=0, agent_run_id=f"run_{task_id}_q0_r1",
                        model="glm", metric_name="correctness", score=0.7, avg_score=0.7, threshold=0.5, passed=True),
        EvaluationResult(task_id=task_id, question_id=1, agent_run_id=f"run_{task_id}_q1_r0",
                        model="glm", metric_name="correctness", score=0.8, avg_score=0.8, threshold=0.5, passed=True),
        EvaluationResult(task_id=task_id, question_id=1, agent_run_id=f"run_{task_id}_q1_r1",
                        model="glm", metric_name="correctness", score=0.6, avg_score=0.6, threshold=0.5, passed=True),
    ])
    session.add(
        ProcessCheckpointTemplate(
            id=f"tpl_cp_{task_id}", logical_agent_id="logical-a", name="规划",
            order=1, required=True, enforce_order=True,
            match_rules='{"mode":"any","rules":[]}', enabled=True,
        )
    )
    session.add_all([
        CheckpointResult(id=f"cpr_{task_id}_q0_r0", agent_run_id=f"run_{task_id}_q0_r0",
                        checkpoint_id=f"tpl_cp_{task_id}", passed=True, evidence={"hit": True, "order_ok": True}),
        CheckpointResult(id=f"cpr_{task_id}_q0_r1", agent_run_id=f"run_{task_id}_q0_r1",
                        checkpoint_id=f"tpl_cp_{task_id}", passed=True, evidence={"hit": True, "order_ok": True}),
        CheckpointResult(id=f"cpr_{task_id}_q1_r0", agent_run_id=f"run_{task_id}_q1_r0",
                        checkpoint_id=f"tpl_cp_{task_id}", passed=True, evidence={"hit": True, "order_ok": True}),
        CheckpointResult(id=f"cpr_{task_id}_q1_r1", agent_run_id=f"run_{task_id}_q1_r1",
                        checkpoint_id=f"tpl_cp_{task_id}", passed=True, evidence={"hit": True, "order_ok": True}),
    ])
    await session.flush()


@pytest.mark.asyncio
async def test_quality_score_multi_round_with_stability(mem_session):
    """多轮场景：完整计算稳定性分（0.5×pass_rate + 0.3×std_component + 0.2×chain_pass_rate）。"""
    await _seed_multi_round_case(mem_session)

    score = await compute_task_quality_score(mem_session, "task_multi")

    # result_score = (0.9+0.7+0.8+0.6)/4 = 0.75
    assert score["result_score"] == pytest.approx(0.75)
    # process_score = 1.0（全 green，必达点全通过）
    assert score["process_score"] == pytest.approx(1.0)
    # pass_rate=1.0, avg_std=0.1, std_component=1-0.1/0.30≈0.6667
    # 质量稳定性 = 1.0×0.6 + 0.6667×0.4 ≈ 0.8667
    # 无运行时指标：稳定性分 = 质量稳定性 = 0.8667
    assert score["stability_score"] == pytest.approx(0.8667, abs=0.01)
    # total = 0.6×0.75 + 0.2×1.0 + 0.2×0.8667 ≈ 0.8233
    assert score["total_score"] == pytest.approx(0.8233, abs=0.01)
    # 三维度全启用
    assert score["result_weight"] == pytest.approx(0.6)
    assert score["process_weight"] == pytest.approx(0.2)
    assert score["stability_weight"] == pytest.approx(0.2)
    # 稳定性细分（v2 新结构）
    assert score["stability_breakdown"]["quality_stability"] == pytest.approx(0.8667, abs=0.01)
    assert score["stability_breakdown"]["performance_stability"] is None
    assert score["gates"] == []


@pytest.mark.asyncio
async def test_quality_score_stability_disabled_transfers_weight(mem_session):
    """enable_stability=False 多轮场景：stability_score=None，0.2 权重并入结果分。"""
    await _seed_multi_round_case(mem_session, task_id="task_stab_off")
    task = await mem_session.get(EvaluationTask, "task_stab_off")
    task.enable_stability = False
    await mem_session.flush()

    score = await compute_task_quality_score(mem_session, "task_stab_off")

    # 稳定性维度未启用 → score=None，权重转移给结果分
    assert score["stability_score"] is None
    assert score["stability_weight"] == pytest.approx(0.0)
    assert score["result_weight"] == pytest.approx(0.8)
    assert score["process_weight"] == pytest.approx(0.2)
    # total = 0.8×0.75 + 0.2×1.0 = 0.8
    assert score["total_score"] == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_quality_score_offline_without_process(mem_session):
    """离线模式无过程分 + 单轮：权重全归结果分。"""
    task_id = "task_offline_simple"
    mem_session.add(
        EvaluationTask(
            id=task_id, dataset_id="ds1", models='["glm"]', status="completed",
            evaluation_mode="offline", agent_id=None,
        )
    )
    mem_session.add(
        EvaluationResult(task_id=task_id, question_id=0, model="glm",
                        metric_name="correctness", score=0.8, avg_score=0.8, threshold=0.5, passed=True),
    )
    await mem_session.flush()

    score = await compute_task_quality_score(mem_session, task_id)

    assert score["evaluation_mode"] == "offline"
    assert score["process_score"] is None
    assert score["process_weight"] == pytest.approx(0.0)
    assert score["stability_score"] is None
    assert score["stability_weight"] == pytest.approx(0.0)
    assert score["result_weight"] == pytest.approx(1.0)
    assert score["total_score"] == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_safety_metrics_excluded_from_result_score(mem_session):
    """安全指标不参与 result_score 加权，但仍在 metric_breakdown 中展示。

    场景：correctness=0.8, faithfulness=0.6, toxicity=1.0（安全指标）
    result_score 只用质量指标加权：(0.8×0.7 + 0.6×0.2)/(0.7+0.2) = 0.7556
    toxicity 不进入加权，但 metric_breakdown 含 toxicity=1.0，metric_weights[toxicity]=0.0
    """
    task_id = "task_safety_excl"
    mem_session.add(
        EvaluationTask(
            id=task_id, dataset_id="ds1", models='["glm"]', status="completed",
            evaluation_mode="offline", agent_id=None,
        )
    )
    mem_session.add_all([
        EvaluationResult(task_id=task_id, question_id=0, model="glm",
                        metric_name="correctness", score=0.8, avg_score=0.8, threshold=0.5, passed=True),
        EvaluationResult(task_id=task_id, question_id=0, model="glm",
                        metric_name="faithfulness", score=0.6, avg_score=0.6, threshold=0.5, passed=True),
        EvaluationResult(task_id=task_id, question_id=0, model="glm",
                        metric_name="toxicity", score=1.0, avg_score=1.0, threshold=0.5, passed=True),
    ])
    await mem_session.flush()

    score = await compute_task_quality_score(mem_session, task_id)

    # result_score 只含质量指标加权（不含 toxicity）
    assert score["result_score"] == pytest.approx(0.7556)
    # metric_breakdown 仍含安全指标
    assert "toxicity" in score["metric_breakdown"]
    assert score["metric_breakdown"]["toxicity"] == pytest.approx(1.0)
    # metric_weights 安全指标=0.0（门禁指标，不参与加权）
    assert score["metric_weights"]["toxicity"] == pytest.approx(0.0)
    assert score["metric_weights"]["correctness"] == pytest.approx(0.7)
    # 安全指标 pass_rate=100% → 不触发 safety gate
    gate_types = [g["type"] for g in score["gates"]]
    assert "safety_pass_rate_low" not in gate_types


@pytest.mark.asyncio
async def test_safety_pass_rate_gate_caps_total(mem_session):
    """安全指标 pass_rate < 60% → 触发 safety_pass_rate_low gate，total_score 封顶 0.50。

    场景：correctness=0.9(passed), faithfulness=0.9(passed), toxicity 2 题中 1 题失败(pass_rate=50%)
    result_score = (0.9×0.7 + 0.9×0.2)/(0.7+0.2) = 0.9
    safety gate: toxicity pass_rate 50% < 60% → cap 0.50
    total = min(raw, 0.50) = 0.50
    """
    task_id = "task_safety_gate"
    mem_session.add(
        EvaluationTask(
            id=task_id, dataset_id="ds1", models='["glm"]', status="completed",
            evaluation_mode="offline", agent_id=None,
        )
    )
    mem_session.add_all([
        EvaluationResult(task_id=task_id, question_id=0, model="glm",
                        metric_name="correctness", score=0.9, avg_score=0.9, threshold=0.5, passed=True),
        EvaluationResult(task_id=task_id, question_id=0, model="glm",
                        metric_name="faithfulness", score=0.9, avg_score=0.9, threshold=0.5, passed=True),
        EvaluationResult(task_id=task_id, question_id=0, model="glm",
                        metric_name="toxicity", score=1.0, avg_score=1.0, threshold=0.5, passed=True),
        EvaluationResult(task_id=task_id, question_id=1, model="glm",
                        metric_name="toxicity", score=0.2, avg_score=0.2, threshold=0.5, passed=False),
    ])
    await mem_session.flush()

    score = await compute_task_quality_score(mem_session, task_id)

    # result_score 不含 toxicity（安全指标）
    assert score["result_score"] == pytest.approx(0.9)
    # safety gate 触发：toxicity pass_rate 50% < 60%
    gate_types = [g["type"] for g in score["gates"]]
    assert "safety_pass_rate_low" in gate_types
    safety_gate = next(g for g in score["gates"] if g["type"] == "safety_pass_rate_low")
    assert safety_gate["cap"] == pytest.approx(0.50)
    # total_score 被封顶到 0.50
    assert score["total_score"] == pytest.approx(0.50)


@pytest.mark.asyncio
async def test_quality_metric_pass_rate_gate_caps_total(mem_session):
    """质量指标 pass_rate < 60%（单轮也触发）→ 触发 quality_metric_pass_rate_low gate。

    场景：correctness 2 题中 1 题失败(pass_rate=50%)，但 avg_score 仍较高
    result_score = (0.7×0.7 + 0.6×0.2)/(0.7+0.2)（toxicity 不参与）
    quality gate: correctness pass_rate 50% < 60% → cap 0.50
    """
    task_id = "task_quality_gate"
    mem_session.add(
        EvaluationTask(
            id=task_id, dataset_id="ds1", models='["glm"]', status="completed",
            evaluation_mode="offline", agent_id=None,
        )
    )
    mem_session.add_all([
        # correctness: 2 题，1 通过 1 失败 → pass_rate=50%
        EvaluationResult(task_id=task_id, question_id=0, model="glm",
                        metric_name="correctness", score=0.9, avg_score=0.9, threshold=0.5, passed=True),
        EvaluationResult(task_id=task_id, question_id=1, model="glm",
                        metric_name="correctness", score=0.5, avg_score=0.5, threshold=0.5, passed=False),
        # faithfulness: 2 题全通过
        EvaluationResult(task_id=task_id, question_id=0, model="glm",
                        metric_name="faithfulness", score=0.9, avg_score=0.9, threshold=0.5, passed=True),
        EvaluationResult(task_id=task_id, question_id=1, model="glm",
                        metric_name="faithfulness", score=0.9, avg_score=0.9, threshold=0.5, passed=True),
    ])
    await mem_session.flush()

    score = await compute_task_quality_score(mem_session, task_id)

    # 原始 result_score = (0.7×0.7 + 0.9×0.2)/(0.7+0.2) = 0.7444
    # correctness avg = (0.9+0.5)/2 = 0.7; faithfulness avg = 0.9
    # quality_metric_pass_rate_low gate 触发：correctness pass_rate 50% < 60%
    # 设计范式：质量类 gate 先封顶 result_score，再传导到 total
    gate_types = [g["type"] for g in score["gates"]]
    assert "quality_metric_pass_rate_low" in gate_types
    quality_gate = next(g for g in score["gates"] if g["type"] == "quality_metric_pass_rate_low")
    assert quality_gate["cap"] == pytest.approx(0.50)
    # result_score 被封顶到 0.50（不再保留原始加权 0.7444）
    assert score["result_score"] == pytest.approx(0.50)
    # raw_total 用封顶后 result_score 重算 = 1.0×0.50 = 0.50（离线单轮，全权重归 result）
    assert score["raw_total_score"] == pytest.approx(0.50)
    # total_score 被封顶到 0.50
    assert score["total_score"] == pytest.approx(0.50)


@pytest.mark.asyncio
async def test_multi_round_correctness_low_uses_quality_metric_gate(mem_session):
    """多轮 correctness pass_rate<60% → 触发 quality_metric_pass_rate_low（cap 0.50），
    不再触发 correctness_pass_rate_low（已删除）。"""
    session = mem_session
    session.add(
        EvaluationTask(
            id="t_multi_cap",
            dataset_id="ds1",
            models='["glm"]',
            status="completed",
            agent_id="ag1",
            evaluation_mode=EVALUATION_MODE_ONLINE,
            inference_rounds=2,
            enable_stability=True,
            created_at=datetime.utcnow(),
        )
    )
    # 2 轮 × 1 题，correctness: r0 通过, r1 失败 → pass_rate=50%
    for idx in range(2):
        session.add(
            AgentRun(
                id=f"run_mc_{idx}",
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t_multi_cap", dataset_id="d1", question_id=0, run_index=idx
                ),
                task_id="t_multi_cap",
                dataset_id="d1",
                question_id=0,
                run_index=idx,
                agent_id="ag1",
                status="success",
                started_at=datetime.utcnow(),
                agent_snapshot="{}",
            )
        )
    session.add_all([
        EvaluationResult(
            task_id="t_multi_cap", question_id=0, model="glm",
            metric_name="correctness", score=0.9, threshold=0.8,
            passed=True, duration_ms=1, avg_score=0.9, agent_run_id="run_mc_0",
        ),
        EvaluationResult(
            task_id="t_multi_cap", question_id=0, model="glm",
            metric_name="correctness", score=0.4, threshold=0.8,
            passed=False, duration_ms=1, avg_score=0.4, agent_run_id="run_mc_1",
        ),
        EvaluationResult(
            task_id="t_multi_cap", question_id=0, model="glm",
            metric_name="faithfulness", score=0.9, threshold=0.8,
            passed=True, duration_ms=1, avg_score=0.9,
        ),
    ])
    await session.commit()

    score = await compute_task_quality_score(session, "t_multi_cap")
    gate_types = [g["type"] for g in score["gates"]]

    # correctness_pass_rate_low 已删除，不应出现
    assert "correctness_pass_rate_low" not in gate_types, (
        f"correctness_pass_rate_low should be deleted, got {gate_types}"
    )
    # quality_metric_pass_rate_low 应触发，cap=0.50
    assert "quality_metric_pass_rate_low" in gate_types
    qg = next(g for g in score["gates"] if g["type"] == "quality_metric_pass_rate_low")
    assert qg["cap"] == pytest.approx(0.50)
    # 设计范式：result_score 被封顶到 0.50（不再保留原始加权值）
    assert score["result_score"] == pytest.approx(0.50)
    # total_score 被封顶到 0.50
    assert score["total_score"] == pytest.approx(0.50)


@pytest.mark.asyncio
async def test_safety_gate_triggers_at_80_percent(mem_session):
    """安全指标 pass_rate=75% < 80%（新阈值）→ 触发 safety_pass_rate_low gate。

    旧阈值 60% 时 75% 不会触发；新阈值 0.80 后应触发。
    """
    session = mem_session
    session.add(
        EvaluationTask(
            id="t_safety_80",
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
            id="run_safety_80",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t_safety_80", dataset_id="d1", question_id=0, run_index=0
            ),
            task_id="t_safety_80",
            dataset_id="d1",
            question_id=0,
            run_index=0,
            agent_id="ag1",
            status="success",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
        )
    )
    # pii_leakage: 4 题中 3 题通过 → pass_rate=75% < 80%（新阈值）
    for qid in range(4):
        session.add(
            EvaluationResult(
                task_id="t_safety_80", question_id=qid, model="glm",
                metric_name="pii_leakage",
                score=1.0 if qid < 3 else 0.4,
                threshold=0.8,
                passed=qid < 3,
                duration_ms=1,
                avg_score=1.0 if qid < 3 else 0.4,
            )
        )
    # 补一条质量指标避免 result_score=None
    for qid in range(4):
        session.add(
            EvaluationResult(
                task_id="t_safety_80", question_id=qid, model="glm",
                metric_name="correctness", score=0.9, threshold=0.8,
                passed=True, duration_ms=1, avg_score=0.9,
            )
        )
    await session.commit()

    score = await compute_task_quality_score(session, "t_safety_80")
    gate_types = [g["type"] for g in score["gates"]]
    assert "safety_pass_rate_low" in gate_types, (
        f"pii_leakage pass_rate=75% should trigger safety gate at 0.80 threshold, got {gate_types}"
    )


@pytest.mark.asyncio
async def test_single_round_stability_disabled(mem_session):
    """单轮推理 → stability_score=None, is_valid=False, stability_weight 并入 result。

    此前单轮 stability=performance_stability（性能达标率），名不副实。
    修复后单轮 stability 维度 disabled，total=result_score。
    """
    session = mem_session
    session.add(
        EvaluationTask(
            id="t_single_stab",
            dataset_id="ds1",
            models='["glm"]',
            status="completed",
            agent_id="ag1",
            evaluation_mode=EVALUATION_MODE_ONLINE,
            inference_rounds=1,
            enable_stability=True,
            created_at=datetime.utcnow(),
        )
    )
    session.add(
        AgentRun(
            id="run_single_stab",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t_single_stab", dataset_id="d1", question_id=0, run_index=0
            ),
            task_id="t_single_stab",
            dataset_id="d1",
            question_id=0,
            run_index=0,
            agent_id="ag1",
            status="success",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
        )
    )
    # 质量指标
    session.add(
        EvaluationResult(
            task_id="t_single_stab", question_id=0, model="glm",
            metric_name="correctness", score=0.85, threshold=0.8,
            passed=True, duration_ms=1, avg_score=0.85,
        )
    )
    # 运行时指标（latency 达标）
    session.add(
        EvaluationResult(
            task_id="t_single_stab", question_id=0, model="glm",
            metric_name="inference_latency", score=1.0, threshold=5000,
            passed=True, duration_ms=1, avg_score=1.0,
            inference_duration_ms=1200, eval_method="runtime",
        )
    )
    await session.commit()

    score = await compute_task_quality_score(session, "t_single_stab")
    # 单轮 → stability 维度 disabled
    assert score["stability_score"] is None, (
        f"single-round stability should be None (disabled), got {score['stability_score']}"
    )
    # stability_weight=0, result_weight=1.0（全部权重并入 result）
    assert score["stability_weight"] == 0.0
    assert score["result_weight"] == pytest.approx(1.0)
    # 无 CheckpointResult 但有 AgentRun → required_total=0（未配置过程点），不触发 process_consistency_fail gate
    gate_types = [g["type"] for g in score["gates"]]
    assert "process_consistency_fail" not in gate_types, (
        f"AgentRun without CheckpointResult should NOT trigger process_consistency_fail gate when required_total=0, got {gate_types}"
    )
    # total = result_score（未被封顶）
    assert score["total_score"] == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_process_consistency_fail_gate_triggers_on_red(mem_session):
    """process_consistency red>0 → 触发 process_consistency_fail gate（cap 0.75）。

    必达过程点未通过（required_total>0 但 required_pass_rate<1.0）→ red=1 → gate 触发。
    """
    session = mem_session
    session.add(
        EvaluationTask(
            id="t_proc_fail_gate",
            dataset_id="ds1",
            models='["glm"]',
            status="completed",
            agent_id="ag1",
            evaluation_mode=EVALUATION_MODE_ONLINE,
            inference_rounds=1,
            enable_process_consistency=True,
            created_at=datetime.utcnow(),
        )
    )
    session.add(
        AgentRun(
            id="run_pfg",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t_proc_fail_gate", dataset_id="d1", question_id=0, run_index=0
            ),
            task_id="t_proc_fail_gate",
            dataset_id="d1",
            question_id=0,
            run_index=0,
            agent_id="ag1",
            status="success",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
        )
    )
    # 质量指标（result_score 正常）
    session.add(
        EvaluationResult(
            task_id="t_proc_fail_gate", question_id=0, model="glm",
            metric_name="correctness", score=0.9, threshold=0.8,
            passed=True, duration_ms=1, avg_score=0.9,
        )
    )
    # 补一条 checkpoint result 让 process_consistency 产出 red=1
    # 注意：需要 ProcessCheckpointTemplate + CheckpointResult
    session.add(
        ProcessCheckpointTemplate(
            id="tpl_pfg",
            logical_agent_id="logical-pfg",
            name="规划",
            order=1,
            required=True,  # 必达但未通过，触发 red
            enforce_order=True,
            match_rules='{"mode":"any","rules":[]}',
            enabled=True,
        )
    )
    session.add(
        CheckpointResult(
            id="cpr_pfg",
            agent_run_id="run_pfg",
            checkpoint_id="tpl_pfg",
            passed=False,
            evidence={"hit": False, "order_ok": False},
        )
    )
    await session.commit()

    score = await compute_task_quality_score(session, "t_proc_fail_gate")
    gate_types = [g["type"] for g in score["gates"]]
    assert "process_consistency_fail" in gate_types, (
        f"process red>0 should trigger process_consistency_fail gate, got {gate_types}"
    )
    pg = next(g for g in score["gates"] if g["type"] == "process_consistency_fail")
    assert pg["cap"] == pytest.approx(0.75)
    # 设计范式：过程类 gate 先封顶 process_score，再传导到 total
    # process_score 被封顶到 ≤ cap（此场景 red=1 → process_score=0.0，本就低于 cap）
    assert score["process_score"] is not None
    assert score["process_score"] <= 0.75 + 1e-6


@pytest.mark.asyncio
async def test_quality_variance_gate_caps_stability_score(mem_session):
    """quality_variance_high gate → 先封顶 stability_score，再传导到 total（设计范式）。

    场景：2 轮 × 2 题，每题 correctness r0=1.0 r1=0.65（均 passed=True）
    → pass_rate=1.0（不触发 quality_metric_pass_rate_low）
    → avg_std≈0.175 > 0.15 触发 quality_variance_high cap=0.70
    → stability_score = 1.0×0.6 + std_component×0.4 ≈ 0.767 > 0.70
    封顶后 stability_score 应 ≤ 0.70。
    """
    session = mem_session
    session.add(
        EvaluationTask(
            id="t_var_gate",
            dataset_id="ds1",
            models='["glm"]',
            status="completed",
            agent_id="ag1",
            evaluation_mode=EVALUATION_MODE_ONLINE,
            inference_rounds=2,
            enable_stability=True,
            created_at=datetime.utcnow(),
        )
    )
    for qid in range(2):
        for idx in range(2):
            session.add(
                AgentRun(
                    id=f"run_vg_{qid}_{idx}",
                    origin="evaluation",
                    eval_context=build_eval_context(
                        task_id="t_var_gate", dataset_id="d1", question_id=qid, run_index=idx
                    ),
                    task_id="t_var_gate",
                    dataset_id="d1",
                    question_id=qid,
                    run_index=idx,
                    agent_id="ag1",
                    status="success",
                    started_at=datetime.utcnow(),
                    agent_snapshot="{}",
                )
            )
    # 2 轮 × 2 题 correctness：r0=1.0 r1=0.65，均 passed
    # 每题 std=0.175 > 0.15，avg_std=0.175 触发 quality_variance_high cap=0.70
    # pass_rate=1.0（不触发 quality_metric_pass_rate_low）
    # std_component = 1 - 0.175/0.30 ≈ 0.417
    # quality_stability = 1.0×0.6 + 0.417×0.4 ≈ 0.767 > 0.70 → 封顶生效
    for qid in range(2):
        session.add(EvaluationResult(
            task_id="t_var_gate", question_id=qid, model="glm",
            metric_name="correctness", score=1.0, threshold=0.5,
            passed=True, duration_ms=1, avg_score=1.0, agent_run_id=f"run_vg_{qid}_0",
        ))
        session.add(EvaluationResult(
            task_id="t_var_gate", question_id=qid, model="glm",
            metric_name="correctness", score=0.65, threshold=0.5,
            passed=True, duration_ms=1, avg_score=0.65, agent_run_id=f"run_vg_{qid}_1",
        ))
    await session.commit()

    score = await compute_task_quality_score(session, "t_var_gate")
    gate_types = [g["type"] for g in score["gates"]]
    assert "quality_variance_high" in gate_types, (
        f"avg_std>0.15 should trigger quality_variance_high gate, got {gate_types}"
    )
    vg = next(g for g in score["gates"] if g["type"] == "quality_variance_high")
    assert vg["cap"] == pytest.approx(0.70)
    # 设计范式：stability_score 被封顶到 ≤ 0.70（原始 ≈0.767）
    assert score["stability_score"] is not None
    assert score["stability_score"] <= 0.70 + 1e-6


@pytest.mark.asyncio
async def test_safety_score_calculation(mem_session):
    """safety_score = 各安全指标 pass_rate 的平均（与 safety gate 同源）。

    场景：toxicity 2 题全通过(pass_rate=1.0), bias 5 题全通过(pass_rate=1.0)
    safety_score = (1.0 + 1.0) / 2 = 1.0
    无安全 gate 触发（均 ≥ 0.80）
    """
    task_id = "task_safety_score"
    mem_session.add(
        EvaluationTask(
            id=task_id, dataset_id="ds1", models='["glm"]', status="completed",
            evaluation_mode="offline", agent_id=None,
        )
    )
    mem_session.add_all([
        # 补质量指标避免 result_score=None
        EvaluationResult(task_id=task_id, question_id=0, model="glm",
                        metric_name="correctness", score=0.9, avg_score=0.9, threshold=0.5, passed=True),
        # toxicity 2 题全通过 → pass_rate=1.0
        EvaluationResult(task_id=task_id, question_id=0, model="glm",
                        metric_name="toxicity", score=1.0, avg_score=1.0, threshold=0.5, passed=True),
        EvaluationResult(task_id=task_id, question_id=1, model="glm",
                        metric_name="toxicity", score=1.0, avg_score=1.0, threshold=0.5, passed=True),
        # bias 2 题全通过 → pass_rate=1.0
        EvaluationResult(task_id=task_id, question_id=0, model="glm",
                        metric_name="bias", score=1.0, avg_score=1.0, threshold=0.5, passed=True),
        EvaluationResult(task_id=task_id, question_id=1, model="glm",
                        metric_name="bias", score=1.0, avg_score=1.0, threshold=0.5, passed=True),
    ])
    await mem_session.flush()

    score = await compute_task_quality_score(mem_session, task_id)

    assert score["safety_score"] == pytest.approx(1.0)
    # 无安全 gate 触发（均 1.0 ≥ 0.80）
    gate_types = [g["type"] for g in score["gates"]]
    assert "safety_pass_rate_low" not in gate_types


@pytest.mark.asyncio
async def test_safety_score_gate_caps_safety_score(mem_session):
    """safety_pass_rate_low gate → 先封顶 safety_score（设计范式）。

    场景：toxicity 4 题中 3 通过(pass_rate=0.75 < 0.80) → gate cap=0.50
    safety_score 原始 = 0.75，封顶后 = 0.50
    total 也 ≤ 0.50（已有逻辑）
    """
    task_id = "task_safety_gate_cap"
    mem_session.add(
        EvaluationTask(
            id=task_id, dataset_id="ds1", models='["glm"]', status="completed",
            evaluation_mode="offline", agent_id=None,
        )
    )
    mem_session.add_all([
        EvaluationResult(task_id=task_id, question_id=0, model="glm",
                        metric_name="correctness", score=0.9, avg_score=0.9, threshold=0.5, passed=True),
        *[EvaluationResult(task_id=task_id, question_id=q, model="glm",
                          metric_name="toxicity", score=1.0 if q < 3 else 0.2,
                          avg_score=1.0 if q < 3 else 0.2, threshold=0.5, passed=q < 3)
          for q in range(4)],
    ])
    await mem_session.flush()

    score = await compute_task_quality_score(mem_session, task_id)

    # safety_score 原始 = 0.75，被封顶到 0.50
    assert score["safety_score"] == pytest.approx(0.50)
    # total 也受 gate 封顶
    assert score["total_score"] == pytest.approx(0.50)


@pytest.mark.asyncio
async def test_safety_score_none_when_no_safety_metrics(mem_session):
    """无安全指标时 safety_score=None。"""
    task_id = "task_no_safety"
    mem_session.add(
        EvaluationTask(
            id=task_id, dataset_id="ds1", models='["glm"]', status="completed",
            evaluation_mode="offline", agent_id=None,
        )
    )
    mem_session.add(
        EvaluationResult(task_id=task_id, question_id=0, model="glm",
                        metric_name="correctness", score=0.9, avg_score=0.9, threshold=0.5, passed=True),
    )
    await mem_session.flush()

    score = await compute_task_quality_score(mem_session, task_id)

    assert score["safety_score"] is None
