"""场景评测测试共用种子数据。"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from database import Agent, AgentRun, Dataset, EvaluationResult, EvaluationTask
from services.dataset_item_scenario_service import item_scenario_key, upsert_item_template
from services.eval_context import build_eval_context

SAMPLE_Q = "协同打击方案 拦截 航路点"
SAMPLE_A = "步骤1：拦截线规划 步骤2：航路点分配 CMD_001 CMD_002"


async def seed_item_template(
    session: AsyncSession,
    *,
    dataset_id: str = "ds_sc",
    dataset_item_id: str = "ditem_test01",
    dataset_name: str = "场景集",
    steps: Optional[List[dict]] = None,
) -> str:
    session.add(
        Dataset(
            id=dataset_id,
            name=dataset_name,
            data=f'[{{"id":"{dataset_item_id}","question":"{SAMPLE_Q}","ground_truth":"{SAMPLE_A}"}}]',
            item_count=1,
            created_at=datetime.utcnow(),
        )
    )
    await session.flush()
    sk = item_scenario_key(dataset_item_id)
    await upsert_item_template(
        session,
        dataset_id=dataset_id,
        dataset_item_id=dataset_item_id,
        name="Q1 测试题",
        steps=steps
        or [
            {
                "step_key": "S1",
                "name": "拦截规划",
                "step_order": 1,
                "weight": 0.5,
                "required": True,
                "gt_expected": {"keywords": ["拦截", "规划"]},
            },
            {
                "step_key": "S2",
                "name": "航路点分配",
                "step_order": 2,
                "weight": 0.5,
                "required": True,
                "gt_expected": {"keywords": ["航路点", "CMD"]},
            },
        ],
    )
    return sk


async def seed_two_tasks_with_item_template(session: AsyncSession) -> str:
    sk = await seed_item_template(session)
    session.add_all(
        [
            Agent(id="ag_s1", name="DemoAgent", version="1.0", inference_config="{}"),
            Agent(id="ag_s2", name="DemoAgent", version="2.0", inference_config="{}"),
        ]
    )
    now = datetime.utcnow()
    session.add_all(
        [
            EvaluationTask(
                id="t_s1",
                dataset_id="ds_sc",
                status="completed",
                agent_id="ag_s1",
                completed_at=now,
            ),
            EvaluationTask(
                id="t_s2",
                dataset_id="ds_sc",
                status="completed",
                agent_id="ag_s2",
                completed_at=now,
            ),
        ]
    )
    qk = "qk_scenario"
    ditem_id = "ditem_test01"
    session.add_all(
        [
            AgentRun(
                id="run_s1",
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t_s1",
                    dataset_id="ds_sc",
                    question_id=0,
                    question_key=qk,
                    dataset_item_id=ditem_id,
                    scenario_key=sk,
                ),
                task_id="t_s1",
                dataset_id="ds_sc",
                question_id=0,
                agent_id="ag_s1",
                status="success",
                started_at=now,
                agent_snapshot="{}",
            ),
            AgentRun(
                id="run_s2",
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t_s2",
                    dataset_id="ds_sc",
                    question_id=0,
                    question_key=qk,
                    dataset_item_id=ditem_id,
                    scenario_key=sk,
                ),
                task_id="t_s2",
                dataset_id="ds_sc",
                question_id=0,
                agent_id="ag_s2",
                status="success",
                started_at=now,
                agent_snapshot="{}",
            ),
        ]
    )
    session.add_all(
        [
            EvaluationResult(
                task_id="t_s1",
                question_id=0,
                model="m",
                metric_name="correctness",
                score=0.5,
                passed=True,
                agent_answer=SAMPLE_A,
                ground_truth=SAMPLE_A,
                question=SAMPLE_Q,
                agent_run_id="run_s1",
            ),
            EvaluationResult(
                task_id="t_s2",
                question_id=0,
                model="m",
                metric_name="correctness",
                score=0.5,
                passed=True,
                agent_answer=SAMPLE_A,
                ground_truth=SAMPLE_A,
                question=SAMPLE_Q,
                agent_run_id="run_s2",
            ),
        ]
    )
    await session.commit()
    return sk
