"""032：迁移 judging_mode 界面词（去"基于"前缀）。

旧名：基于Ground_Truth评测
新名：Ground_Truth评测

将 028 迁移产生的中间名 "基于Ground_Truth评测" 统一替换为最终名 "Ground_Truth评测"。
"基于Prompt评测" 保持不变。仅作用于 datasets.data JSON 中的 judging_mode /
step_judging_mode 字段。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import json


revision: str = "032_migrate_judging_mode_drop_prefix"
down_revision: Union[str, None] = "031_chat_pending_confirm"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 旧名 → 新名映射
_MODE_RENAME = {
    "基于Ground_Truth评测": "Ground_Truth评测",
}


def _migrate_dataset_data(data_json: str) -> str:
    """替换 Dataset.data JSON 中的 judging_mode / step_judging_mode 旧名。"""
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
        steps = item.get("steps")
        if isinstance(steps, list):
            for s in steps:
                if not isinstance(s, dict):
                    continue
                sjm = s.get("step_judging_mode")
                if sjm in _MODE_RENAME:
                    s["step_judging_mode"] = _MODE_RENAME[sjm]
                    changed = True

    if changed:
        return json.dumps(data, ensure_ascii=False)
    return data_json


def upgrade() -> None:
    bind = op.get_bind()
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
            steps = item.get("steps")
            if isinstance(steps, list):
                for s in steps:
                    if not isinstance(s, dict):
                        continue
                    sjm = s.get("step_judging_mode")
                    if sjm in reverse_rename:
                        s["step_judging_mode"] = reverse_rename[sjm]
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
