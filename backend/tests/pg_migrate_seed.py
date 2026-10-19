"""PG migrate 用 SQLite 源库种子数据（A-06 / 集成 smoke 共享）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import sessionmaker

from database import (
    Agent,
    AgentRun,
    Base,
    CheckpointResult,
    Dataset,
    EvaluationResult,
    EvaluationRound,
    EvaluationTask,
    RunMetric,
    RunSpanNode,
    TraceSyncJob,
)


def seed_sqlite_source(engine) -> str:
    """创建并填充 SQLite 源库，返回 sample agent_run_id。"""
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    now = datetime.utcnow()
    run_id = "run_pg_smoke_1"

    with Session() as session:
        session.add(
            Dataset(
                id="ds_pg_smoke",
                name="PG Smoke DS",
                data='[{"question":"q","ground_truth":"gt"}]',
                item_count=1,
                created_at=now,
            )
        )
        session.add(
            Agent(
                id="ag_pg_smoke",
                name="Smoke Agent",
                created_at=now,
            )
        )
        session.add(
            EvaluationTask(
                id="task_pg_smoke",
                dataset_id="ds_pg_smoke",
                models='["default"]',
                status="completed",
                agent_id="ag_pg_smoke",
                created_at=now,
            )
        )
        session.add(
            AgentRun(
                id=run_id,
                origin="evaluation",
                eval_context={
                    "task_id": "task_pg_smoke",
                    "dataset_id": "ds_pg_smoke",
                    "question_id": 0,
                    "run_index": 0,
                    "dataset_item_id": "ditem_smoke",
                },
                task_id="task_pg_smoke",
                dataset_id="ds_pg_smoke",
                question_id=0,
                agent_id="ag_pg_smoke",
                trace_id="tr_pg_smoke",
                status="success",
                started_at=now,
                tokens_total=42,
                input_snapshot={"question": "q"},
                output_snapshot={"model_answer": "a"},
                trace_sync_status="synced",
                agent_snapshot="{}",
            )
        )
        session.add(
            RunSpanNode(
                id="span_pg_smoke",
                agent_run_id=run_id,
                observation_id="obs_1",
                sibling_order=0,
                step_index=0,
                node_type="span",
                name="step",
                attributes={"agent.step.id": "main"},
            )
        )
        session.add(
            RunMetric(
                id="metric_pg_smoke",
                agent_run_id=run_id,
                metric_key="runtime.tokens_total",
                source="runtime",
                value_num=42.0,
                dims_hash="",
            )
        )
        session.add(
            CheckpointResult(
                id="cp_pg_smoke",
                agent_run_id=run_id,
                checkpoint_id="ck_tpl",
                passed=True,
                evidence={"matched": True},
            )
        )
        session.add(
            TraceSyncJob(
                id="tsj_pg_smoke",
                agent_run_id=run_id,
                task_id="task_pg_smoke",
                trace_id="tr_pg_smoke",
                status="success",
                attempts=1,
                created_at=now,
            )
        )
        result_row = EvaluationResult(
            task_id="task_pg_smoke",
            question_id=0,
            model="glm",
            metric_name="faithfulness",
            score=0.9,
            threshold=0.8,
            passed=True,
            agent_run_id=run_id,
            avg_score=0.9,
        )
        session.add(result_row)
        session.flush()
        session.add(
            EvaluationRound(
                result_id=result_row.id,
                round_num=1,
                score=0.9,
                reason="ok",
            )
        )
        session.commit()

    return run_id
