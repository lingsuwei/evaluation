"""018：evaluation_tasks.ontology_package（BM25 评估使用的本体包名称）。"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "018_task_ontology_package"
down_revision: Union[str, None] = "017_task_langfuse_source"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    if not _has_column("evaluation_tasks", "ontology_package"):
        op.add_column(
            "evaluation_tasks",
            sa.Column("ontology_package", sa.String(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("evaluation_tasks", "ontology_package")
