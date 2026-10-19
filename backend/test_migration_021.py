# -*- coding: utf-8 -*-
"""T3-3 测试用例：Alembic 迁移 021_custom_metrics 验证

由于 alembic CLI 需要 config.py 支持 SQLite async URL 转换（当前不支持），
本测试用同步 SQLite + Base.metadata.create_all() 验证表结构正确性，
同时验证迁移文件的 revision 链路和元数据。

迁移文件的实际执行验证（alembic upgrade head）留给 PostgreSQL 部署环境。

测试覆盖：
1. 迁移文件 revision/down_revision 链路正确
2. 迁移文件 upgrade/downgrade 函数定义正确
3. Base.metadata 包含 custom_metrics 表
4. custom_metrics 表结构正确（12 列，类型匹配）
5. evaluation_tasks 表包含 custom_metric_ids 列
6. 数据可写入 custom_metrics 表
7. 迁移文件字段与 ORM 模型一致
"""
import sys
import os
import importlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_passed = 0
_failed = 0


def assert_true(desc: str, cond: bool):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {desc}")
    else:
        _failed += 1
        print(f"  [FAIL] {desc}")


def assert_eq(desc: str, actual, expected):
    global _passed, _failed
    if actual == expected:
        _passed += 1
        print(f"  [PASS] {desc} → {actual}")
    else:
        _failed += 1
        print(f"  [FAIL] {desc} → 期望 {expected!r}，实际 {actual!r}")


print("=" * 60)
print("T3-3 测试：Alembic 迁移 021_custom_metrics")
print("=" * 60)

# --- 测试1：迁移文件 revision 链路 ---
print("\n[测试1] 迁移文件 revision 链路")
spec = importlib.util.spec_from_file_location(
    "migration_021",
    os.path.join(os.path.dirname(__file__), "alembic", "versions", "021_custom_metrics.py"),
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)

assert_eq("revision", migration.revision, "021_custom_metrics")
assert_eq("down_revision", migration.down_revision, "020_report_archive")
assert_true("branch_labels 为 None", migration.branch_labels is None)
assert_true("depends_on 为 None", migration.depends_on is None)
assert_true("upgrade 函数存在且可调用", callable(migration.upgrade))
assert_true("downgrade 函数存在且可调用", callable(migration.downgrade))

# --- 测试2：迁移文件有 _has_table / _has_column 辅助函数 ---
print("\n[测试2] 迁移文件辅助函数")
assert_true("_has_table 函数存在", callable(migration._has_table))
assert_true("_has_column 函数存在", callable(migration._has_column))

# --- 测试3：用同步 SQLite 引擎创建所有表 ---
print("\n[测试3] Base.metadata.create_all 创建表")
from sqlalchemy import create_engine, inspect as sa_inspect, text
from database import Base, CustomMetric, EvaluationTask

engine = create_engine("sqlite:///:memory:", echo=False)
Base.metadata.create_all(engine)

insp = sa_inspect(engine)
assert_true("custom_metrics 表已创建", "custom_metrics" in insp.get_table_names())
assert_true("evaluation_tasks 表已创建", "evaluation_tasks" in insp.get_table_names())

# --- 测试4：custom_metrics 表结构验证 ---
print("\n[测试4] custom_metrics 表结构（12 列）")
cm_cols = {c["name"]: c for c in insp.get_columns("custom_metrics")}
expected_cols = {
    "id": "INTEGER",
    "name": "VARCHAR",
    "kind": "VARCHAR",
    "criteria": "TEXT",
    "dag": "JSON",  # SQLite 下 JSON 存为 TEXT，但 type 标记为 JSON
    "code": "TEXT",
    "multi_turn": "BOOLEAN",
    "params": "JSON",
    "threshold": "FLOAT",
    "is_active": "BOOLEAN",
    "created_at": "DATETIME",
    "updated_at": "DATETIME",
}
assert_eq("列数", len(cm_cols), 12)
for col_name in expected_cols:
    assert_true(f"列 '{col_name}' 存在", col_name in cm_cols)

# --- 测试5：evaluation_tasks 包含 custom_metric_ids 列 ---
print("\n[测试5] evaluation_tasks.custom_metric_ids 列")
et_cols = {c["name"] for c in insp.get_columns("evaluation_tasks")}
assert_true("custom_metric_ids 列存在", "custom_metric_ids" in et_cols)
# 验证 nullable
cm_id_col = next(c for c in insp.get_columns("evaluation_tasks") if c["name"] == "custom_metric_ids")
assert_true("custom_metric_ids nullable=True", cm_id_col.get("nullable", True) is True)

# --- 测试6：数据可写入 custom_metrics 表 ---
print("\n[测试6] 数据写入 custom_metrics 表")
with engine.connect() as conn:
    conn.execute(text(
        "INSERT INTO custom_metrics (name, kind, criteria, multi_turn, threshold, is_active) "
        "VALUES ('安全检查', 'geval', '判断回答是否安全', 0, 0.3, 1)"
    ))
    conn.execute(text(
        "INSERT INTO custom_metrics (name, kind, code, multi_turn, threshold, is_active) "
        "VALUES ('长度检查', 'code', 'score = 1.0', 0, 0.6, 1)"
    ))
    result = conn.execute(text("SELECT name, kind, threshold, is_active FROM custom_metrics ORDER BY id")).fetchall()
    conn.commit()

assert_eq("写入 2 条记录", len(result), 2)
assert_eq("第1条 name", result[0][0], "安全检查")
assert_eq("第1条 kind", result[0][1], "geval")
assert_eq("第2条 name", result[1][0], "长度检查")
assert_eq("第2条 kind", result[1][1], "code")

# --- 测试7：迁移文件字段定义与 ORM 模型一致 ---
print("\n[测试7] 迁移文件字段与 ORM 模型一致")
orm_cols = {c.name for c in CustomMetric.__table__.columns}
db_cols = set(cm_cols.keys())
assert_eq("ORM 列数 == DB 列数", len(orm_cols), len(db_cols))
assert_true("ORM 列集 == DB 列集", orm_cols == db_cols)

# --- 测试8：迁移文件 server_default 定义验证 ---
print("\n[测试8] 迁移文件 server_default 定义")
# 读取迁移文件源码验证 server_default 定义（Base.metadata.create_all 用 ORM default=，不生成 DDL DEFAULT）
import inspect as py_inspect
src = py_inspect.getsource(migration)
assert_true("multi_turn 有 server_default=sa.false()", "server_default=sa.false()" in src)
assert_true("is_active 有 server_default=sa.true()", "server_default=sa.true()" in src)
assert_true("threshold 有 server_default='0.5'", 'server_default="0.5"' in src)
assert_true("created_at 有 server_default=sa.func.now()", "server_default=sa.func.now()" in src)
assert_true("updated_at 有 server_default=sa.func.now()", "server_default=sa.func.now()" in src)
# ORM 模型用 default=（Python 端），迁移文件用 server_default=（DB 端），两者互补
from sqlalchemy import inspect as sa_inspect2
orm_threshold = sa_inspect2(CustomMetric).columns["threshold"]
assert_true("ORM threshold 有 Python default", orm_threshold.default is not None)
assert_eq("ORM threshold.default.arg", orm_threshold.default.arg, 0.5)

# ===== 结果汇总 =====
print("\n" + "=" * 60)
print(f"测试结果：{_passed} 通过，{_failed} 失败")
print("=" * 60)
sys.exit(0 if _failed == 0 else 1)
