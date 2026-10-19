"""019：scenario_eval_templates.source（子步骤来源追溯）。

注：该迁移此前已应用于线上库但文件缺失，现恢复以保持 alembic 版本链完整。
幂等：source 列已存在时跳过。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "019_scenario_template_source"
down_revision: Union[str, None] = "018_task_ontology_package"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    if not _has_column("scenario_eval_templates", "source"):
        op.add_column(
            "scenario_eval_templates",
            sa.Column("source", sa.String(), nullable=True),
        )
        # 已有模板默认标记为 manual
        op.execute(
            "UPDATE scenario_eval_templates SET source = 'manual' WHERE source IS NULL"
        )


def downgrade() -> None:
    op.drop_column("scenario_eval_templates", "source")
