"""物化后持久化场景子步骤 ↔ observation 绑定（ST-3-3）。"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import RunSpanNode, ScenarioStepSpanRef


async def persist_scenario_step_span_refs(
    session: AsyncSession,
    agent_run_id: str,
    step_results: List[Dict[str, Any]],
) -> None:
    """将 compute_run_scenario_steps 的绑定结果写入 scenario_step_span_refs。"""
    step_ids = [str(s.get("step_id") or "") for s in step_results if s.get("step_id")]
    if not step_ids:
        return
    await session.execute(
        delete(ScenarioStepSpanRef).where(
            ScenarioStepSpanRef.agent_run_id == agent_run_id,
            ScenarioStepSpanRef.step_id.in_(step_ids),
        )
    )
    for item in step_results:
        step_id = item.get("step_id")
        if not step_id:
            continue
        tb = item.get("trace_binding") if isinstance(item.get("trace_binding"), dict) else {}
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        obs_id = tb.get("observation_id") or evidence.get("observation_id")
        bind_source = evidence.get("bind_source")
        span_node_id: Optional[str] = None
        if obs_id:
            row = (
                await session.execute(
                    select(RunSpanNode)
                    .where(
                        RunSpanNode.agent_run_id == agent_run_id,
                        RunSpanNode.observation_id == str(obs_id),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row:
                span_node_id = row.id
        session.add(
            ScenarioStepSpanRef(
                id=f"sssr_{uuid.uuid4().hex[:12]}",
                agent_run_id=agent_run_id,
                step_id=str(step_id),
                observation_id=str(obs_id) if obs_id else None,
                span_node_id=span_node_id,
                bind_source=str(bind_source) if bind_source else None,
                bind_meta={
                    "step_key": item.get("step_key"),
                    "observation_name": tb.get("observation_name"),
                    "bind_source": bind_source,
                },
            )
        )
    await session.flush()


async def list_scenario_step_span_refs(
    session: AsyncSession,
    agent_run_id: str,
) -> List[Dict[str, Any]]:
    """读取物化的子步骤 ↔ Span 绑定（E-3）。"""
    from database import ScenarioEvalStep

    rows = (
        await session.execute(
            select(ScenarioStepSpanRef, ScenarioEvalStep)
            .join(
                ScenarioEvalStep,
                ScenarioEvalStep.id == ScenarioStepSpanRef.step_id,
            )
            .where(ScenarioStepSpanRef.agent_run_id == agent_run_id)
            .order_by(ScenarioEvalStep.step_order)
        )
    ).all()
    out: List[Dict[str, Any]] = []
    for ref, step in rows:
        meta = ref.bind_meta if isinstance(ref.bind_meta, dict) else {}
        out.append(
            {
                "id": ref.id,
                "step_id": ref.step_id,
                "step_key": step.step_key,
                "step_name": step.name,
                "observation_id": ref.observation_id,
                "span_node_id": ref.span_node_id,
                "bind_source": ref.bind_source,
                "bind_meta": meta,
            }
        )
    return out
