"""028：迁移 judging_mode 界面词。

旧名：关键词评测 / 提示词评测
新名：基于Ground_Truth评测 / 基于Prompt评测

旧名容易误解（"关键词"像正则匹配），新名直接对应数据集字段 Ground_Truth / Prompt。
此迁移将 Dataset.data JSON 中已有的旧名替换为新名，确保导出数据集时显示新名称。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import json


revision: str = "028_migrate_judging_mode_names"
down_revision: Union[str, None] = "027_ssr_trace_binding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 旧名 → 新名映射
_MODE_RENAME = {
    "关键词评测": "基于Ground_Truth评测",
    "提示词评测": "基于Prompt评测",
}


def _migrate_dataset_data(data_json: str) -> str:
    """替换 Dataset.data JSON 中的 judging_mode 旧名。"""
    if not data_json:
        return data_json
    try:
        data = json.loads(data_json)
    except (json.JSONDecodeError, TypeError):
        return data_json

    if not isinstance(data, list):
        return data_json

    changed = False
    for item in data:
        if not isinstance(item, dict):
            continue
        jm = item.get("judging_mode")
        if jm in _MODE_RENAME:
            item["judging_mode"] = _MODE_RENAME[jm]
            changed = True

    if changed:
        return json.dumps(data, ensure_ascii=False)
    return data_json


def upgrade() -> None:
    bind = op.get_bind()
    # 直接 SELECT + UPDATE datasets 表的 data 列
    result = bind.execute(sa.text("SELECT id, data FROM datasets"))
    rows = result.fetchall()

    for row in rows:
        ds_id, data_json = row
        new_data = _migrate_dataset_data(data_json)
        if new_data != data_json:
            bind.execute(
                sa.text("UPDATE datasets SET data = :data WHERE id = :id"),
                {"data": new_data, "id": ds_id},
            )


def downgrade() -> None:
    """回滚：新名 → 旧名。"""
    reverse_rename = {v: k for k, v in _MODE_RENAME.items()}

    def _reverse_migrate(data_json: str) -> str:
        if not data_json:
            return data_json
        try:
            data = json.loads(data_json)
        except (json.JSONDecodeError, TypeError):
            return data_json

        if not isinstance(data, list):
            return data_json

        changed = False
        for item in data:
            if not isinstance(item, dict):
                continue
            jm = item.get("judging_mode")
            if jm in reverse_rename:
                item["judging_mode"] = reverse_rename[jm]
                changed = True

        if changed:
            return json.dumps(data, ensure_ascii=False)
        return data_json

    bind = op.get_bind()
    result = bind.execute(sa.text("SELECT id, data FROM datasets"))
    rows = result.fetchall()

    for row in rows:
        ds_id, data_json = row
        new_data = _reverse_migrate(data_json)
        if new_data != data_json:
            bind.execute(
                sa.text("UPDATE datasets SET data = :data WHERE id = :id"),
                {"data": new_data, "id": ds_id},
            )
