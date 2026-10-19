"""P2 F-04：v_run_analytics 读模型视图（RF-01）

Revision ID: 002_v_run_analytics
Revises: 001_p2_fact
Create Date: 2026-05-23

"""
from typing import Sequence, Union

from alembic import op

revision: str = "002_v_run_analytics"
down_revision: Union[str, None] = "001_p2_fact"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 与 services/run_analytics_view.py 保持一致
V_RUN_ANALYTICS_SQL = """
CREATE OR REPLACE VIEW v_run_analytics AS
SELECT
    ar.id AS agent_run_id,
    COALESCE(ar.task_id, ar.eval_context->>'task_id') AS task_id,
    COALESCE(ar.dataset_id, ar.eval_context->>'dataset_id') AS dataset_id,
    COALESCE(
        ar.question_id,
        NULLIF(ar.eval_context->>'question_id', '')::integer
    ) AS question_id,
    COALESCE(
        ar.run_index,
        NULLIF(ar.eval_context->>'run_index', '')::integer,
        0
    ) AS run_index,
    ar.agent_id,
    ar.logical_agent_id,
    ar.agent_name,
    ar.agent_version,
    ar.status,
    ar.trace_id,
    ar.trace_sync_status,
    ar.tokens_total,
    ar.started_at,
    ar.ended_at,
    MAX(rm.value_num) FILTER (WHERE rm.metric_key = 'runtime.tokens_total')
        AS runtime_tokens_total,
    MAX(rm.value_num) FILTER (WHERE rm.metric_key = 'runtime.latency_ms')
        AS runtime_latency_ms,
    MAX(rm.value_num) FILTER (WHERE rm.metric_key = 'runtime.observation_count')
        AS runtime_observation_count,
    COALESCE(
        jsonb_object_agg(rm.metric_key, rm.value_num)
            FILTER (WHERE rm.metric_key LIKE 'eval.%' AND rm.value_num IS NOT NULL),
        '{}'::jsonb
    ) AS eval_scores,
    COUNT(DISTINCT cpr.id)::integer AS checkpoint_total,
    COUNT(DISTINCT cpr.id) FILTER (WHERE cpr.passed IS TRUE)::integer AS checkpoint_passed
FROM agent_runs ar
LEFT JOIN run_metrics rm ON rm.agent_run_id = ar.id
LEFT JOIN checkpoint_results cpr ON cpr.agent_run_id = ar.id
GROUP BY ar.id;
"""


def upgrade() -> None:
    op.execute(V_RUN_ANALYTICS_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_run_analytics")
