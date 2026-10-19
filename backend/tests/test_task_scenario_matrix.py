"""单任务场景子步骤矩阵（P1）。"""
from __future__ import annotations

from datetime import datetime

import json
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import (
    Agent,
    AgentRun,
    Base,
    Dataset,
    EvaluationResult,
    EvaluationTask,
    ProcessCheckpointTemplate,
    RunSpanNode,
    ScenarioEvalStep,
    ScenarioEvalTemplate,
)
from services.cross_task_cockpit_service import CrossTaskCockpitError
from services.eval_context import build_eval_context
from services.task_scenario_matrix_service import build_task_scenario_matrix
from tests.scenario_test_helpers import SAMPLE_A, SAMPLE_Q, seed_item_template

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


async def _seed_intra_task_two_agents(session: AsyncSession):
    sk = await seed_item_template(session, dataset_id="ds_intra", dataset_name="Intra")
    session.add_all(
        [
            Agent(id="ag_i1", name="Demo", version="1.0", inference_config="{}"),
            Agent(id="ag_i2", name="Demo", version="2.0", inference_config="{}"),
        ]
    )
    now = datetime.utcnow()
    session.add(
        EvaluationTask(
            id="t_intra",
            dataset_id="ds_intra",
            status="completed",
            agent_id="ag_i1",
            completed_at=now,
        )
    )
    qk = "qk_intra"
    ditem_id = "ditem_test01"
    session.add_all(
        [
            AgentRun(
                id="run_i1",
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t_intra",
                    dataset_id="ds_intra",
                    question_id=0,
                    question_key=qk,
                    dataset_item_id=ditem_id,
                    scenario_key=sk,
                ),
                task_id="t_intra",
                dataset_id="ds_intra",
                question_id=0,
                agent_id="ag_i1",
                logical_agent_id="lg_demo",
                status="success",
                started_at=now,
                agent_snapshot="{}",
            ),
            AgentRun(
                id="run_i2",
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t_intra",
                    dataset_id="ds_intra",
                    question_id=0,
                    question_key=qk,
                    dataset_item_id=ditem_id,
                    scenario_key=sk,
                ),
                task_id="t_intra",
                dataset_id="ds_intra",
                question_id=0,
                agent_id="ag_i2",
                logical_agent_id="lg_demo",
                status="success",
                started_at=now,
                agent_snapshot="{}",
            ),
        ]
    )
    session.add_all(
        [
            EvaluationResult(
                task_id="t_intra",
                question_id=0,
                metric_name="correctness",
                score=0.5,
                passed=True,
                agent_answer=SAMPLE_A,
                ground_truth=SAMPLE_A,
                question=SAMPLE_Q,
                agent_run_id="run_i1",
            ),
            EvaluationResult(
                task_id="t_intra",
                question_id=0,
                metric_name="correctness",
                score=0.5,
                passed=True,
                agent_answer=SAMPLE_A,
                ground_truth=SAMPLE_A,
                question=SAMPLE_Q,
                agent_run_id="run_i2",
            ),
        ]
    )
    await session.commit()
    return sk


@pytest.mark.asyncio
async def test_task_scenario_matrix_explicit_item_template_two_agents(mem_session):
    sk = await _seed_intra_task_two_agents(mem_session)
    payload = await build_task_scenario_matrix(mem_session, "t_intra", scenario_key=sk)
    assert payload["comparison_mode"] == "intra_task_scenario"
    assert len(payload["member_ids"]) == 2
    row = payload["questions"][0]
    assert row["cells"]["ag_i1"] is not None
    assert row["cells"]["ag_i2"] is not None
    # 单轮场景 cell 必须返回 runs 数组（与多轮 aggregate_scenario_cell 对齐），
    # 否则前端 ScenarioDeliveryView 迭代 cell.runs 会触发 "X.runs is not iterable"
    assert isinstance(row["cells"]["ag_i1"].get("runs"), list)
    assert len(row["cells"]["ag_i1"]["runs"]) == 1
    assert row["cells"]["ag_i1"]["runs"][0]["agent_run_id"] is not None


@pytest.mark.asyncio
async def test_task_scenario_matrix_auto_mode_uses_item_template(mem_session):
    await _seed_intra_task_two_agents(mem_session)
    payload = await build_task_scenario_matrix(mem_session, "t_intra")
    assert len(payload["step_columns"]) == 2
    assert payload["empty_reason"] is None
    assert payload["questions"][0]["cells"]["ag_i1"] is not None
    assert payload["questions"][0]["cells"]["ag_i1"]["composite_score"] > 0


@pytest.mark.asyncio
async def test_task_scenario_matrix_prefers_item_steps_over_checkpoints(mem_session):
    """场景子步骤矩阵应读评测题场景模板，不因 Agent 过程点配置而替换。"""
    sk = await _seed_intra_task_two_agents(mem_session)
    mem_session.add(
        ProcessCheckpointTemplate(
            id="chk_judge",
            logical_agent_id="lg_demo",
            name="拦截规划",
            description="识别态势并输出研判结论",
            order=1,
            required=True,
            match_rules=json.dumps(
                {
                    "mode": "any",
                    "rules": [
                        {
                            "type": "metadata",
                            "key": "agent.step.id",
                            "op": "eq",
                            "value": "judge",
                        }
                    ],
                }
            ),
            score_weight=1.0,
            enabled=True,
        )
    )
    mem_session.add(
        RunSpanNode(
            id="span_judge",
            agent_run_id="run_i1",
            observation_id="obs_judge",
            sibling_order=0,
            step_index=0,
            node_type="span",
            name="judge",
            attributes={"agent.step.id": "judge"},
        )
    )
    await mem_session.commit()

    payload = await build_task_scenario_matrix(mem_session, "t_intra")
    assert payload["empty_reason"] is None
    assert payload["template"]["name"] == "按题场景子步骤"
    step_keys = [c["step_key"] for c in payload["step_columns"]]
    assert step_keys == ["S1", "S2"]
    assert payload["questions"][0]["cells"]["ag_i1"]["composite_score"] > 0
    assert payload["questions"][0]["cells"]["ag_i1"]["steps"][0].get(
        "evidence", {}
    ).get("source") != "process_checkpoint"


@pytest.mark.asyncio
async def test_task_scenario_matrix_no_runs(mem_session):
    mem_session.add(
        EvaluationTask(
            id="t_empty",
            dataset_id="ds_x",
            status="completed",
            completed_at=datetime.utcnow(),
        )
    )
    await mem_session.commit()
    with pytest.raises(CrossTaskCockpitError, match="无可用"):
        await build_task_scenario_matrix(mem_session, "t_empty")


@pytest.mark.asyncio
async def test_task_scenario_matrix_warns_when_llm_aggregate_unconfigured(
    mem_session, monkeypatch
):
    """启用 llm_aggregate 但未配置 API Key 时，矩阵应返回警告且不打 500。"""
    from services.dataset_item_scenario_service import upsert_item_template

    monkeypatch.setattr(
        "config.get_llm_config",
        lambda _key=None: {"api_key": ""},
    )
    ditem_id = "ditem_llm_warn"
    mem_session.add(
        Dataset(
            id="ds_llm_warn",
            name="LLM warn ds",
            data=json.dumps([{"id": ditem_id, "question": "q"}]),
            item_count=1,
        )
    )
    mem_session.add(
        Agent(id="ag_llm", name="Demo", version="1.0", inference_config="{}")
    )
    await mem_session.flush()
    await upsert_item_template(
        mem_session,
        dataset_id="ds_llm_warn",
        dataset_item_id=ditem_id,
        name="LLM warn",
        steps=[
            {
                "step_key": "S1",
                "name": "LLM步",
                "step_order": 1,
                "weight": 1.0,
                "required": True,
                "enabled": True,
                "gt_expected": {"keywords": ["ok"]},
                "match_rules": {
                    "eval_config": {
                        "mode": "llm_aggregate",
                        "aggregate_prompt": "请汇总",
                    },
                    "trace_binding": {
                        "span_name_patterns": ["answer"],
                        "output_keywords": ["ok"],
                    },
                },
            }
        ],
    )
    now = datetime.utcnow()
    mem_session.add(
        EvaluationTask(
            id="t_llm_warn",
            dataset_id="ds_llm_warn",
            status="completed",
            agent_id="ag_llm",
            completed_at=now,
        )
    )
    mem_session.add(
        AgentRun(
            id="run_llm_warn",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t_llm_warn",
                dataset_id="ds_llm_warn",
                question_id=0,
                question_key="qk_llm",
                dataset_item_id=ditem_id,
            ),
            task_id="t_llm_warn",
            dataset_id="ds_llm_warn",
            question_id=0,
            agent_id="ag_llm",
            status="success",
            started_at=now,
            agent_snapshot="{}",
        )
    )
    mem_session.add(
        EvaluationResult(
            task_id="t_llm_warn",
            question_id=0,
            metric_name="correctness",
            score=1.0,
            passed=True,
            agent_answer="ok answer",
            ground_truth="ok",
            question="q",
            agent_run_id="run_llm_warn",
        )
    )
    await mem_session.commit()

    payload = await build_task_scenario_matrix(mem_session, "t_llm_warn")
    assert any("基于Prompt评测" in w for w in payload.get("warnings") or [])
    assert payload["questions"][0]["cells"]["ag_llm"] is not None


@pytest.mark.asyncio
async def test_task_scenario_matrix_warns_incomplete_llm_aggregate_config(mem_session):
    """llm_aggregate 缺 keywords 时矩阵应返回配置不完整警告（含历史脏数据）。"""
    from services.dataset_item_scenario_service import item_scenario_key
    from database import ScenarioEvalTemplate, ScenarioEvalStep

    ditem_id = "ditem_incomplete"
    mem_session.add(
        Dataset(
            id="ds_incomplete",
            name="incomplete",
            data=json.dumps([{"id": ditem_id, "question": "q"}]),
            item_count=1,
        )
    )
    mem_session.add(
        Agent(id="ag_inc", name="Demo", version="1.0", inference_config="{}")
    )
    tpl = ScenarioEvalTemplate(
        id="scen_inc",
        scenario_key=item_scenario_key(ditem_id),
        name="Incomplete",
        dataset_id="ds_incomplete",
        dataset_item_id=ditem_id,
        enabled=True,
    )
    mem_session.add(tpl)
    await mem_session.flush()
    mem_session.add(
        ScenarioEvalStep(
            id="step_inc_S1",
            template_id=tpl.id,
            step_key="S1",
            name="LLM步",
            step_order=1,
            weight=1.0,
            required=True,
            enabled=True,
            gt_expected={"keywords": []},
            match_rules={
                "eval_config": {
                    "mode": "llm_aggregate",
                    "aggregate_prompt": "请汇总",
                }
            },
        )
    )
    now = datetime.utcnow()
    mem_session.add(
        EvaluationTask(
            id="t_inc",
            dataset_id="ds_incomplete",
            status="completed",
            agent_id="ag_inc",
            completed_at=now,
        )
    )
    mem_session.add(
        AgentRun(
            id="run_inc",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t_inc",
                dataset_id="ds_incomplete",
                question_id=0,
                dataset_item_id=ditem_id,
            ),
            task_id="t_inc",
            dataset_id="ds_incomplete",
            question_id=0,
            agent_id="ag_inc",
            status="success",
            started_at=now,
            agent_snapshot="{}",
        )
    )
    mem_session.add(
        EvaluationResult(
            task_id="t_inc",
            question_id=0,
            metric_name="correctness",
            score=1.0,
            passed=True,
            agent_answer="ok",
            ground_truth="ok",
            question="q",
            agent_run_id="run_inc",
        )
    )
    await mem_session.commit()

    payload = await build_task_scenario_matrix(mem_session, "t_inc")
    assert any("配置不完整" in w for w in payload.get("warnings") or [])
