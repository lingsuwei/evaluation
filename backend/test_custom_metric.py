# -*- coding: utf-8 -*-
"""T3-1 测试用例：CustomMetric ORM 模型验证

测试覆盖：
1. 模型导入与表名
2. 字段存在性与类型
3. 实例创建与默认值
4. 三种 kind 模式的字段填充
5. 回归：现有 ORM 模型不受影响
"""
import sys
import os

# 确保能 import backend 模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ===== 测试工具 =====
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


# ===== 测试开始 =====
print("=" * 60)
print("T3-1 测试：CustomMetric ORM 模型")
print("=" * 60)

# --- 测试1：模型导入与表名 ---
print("\n[测试1] 模型导入与表名")
try:
    from database import CustomMetric, Base
    assert_true("CustomMetric 类可导入", CustomMetric is not None)
    assert_eq("表名", CustomMetric.__tablename__, "custom_metrics")
    assert_true("CustomMetric 是 Base 子类", issubclass(CustomMetric, Base))
except Exception as e:
    assert_true(f"导入失败: {e}", False)
    sys.exit(1)

# --- 测试2：字段存在性与类型 ---
print("\n[测试2] 字段存在性与类型")
from sqlalchemy import Integer, String, Text, JSON, Boolean, Float, DateTime, Column

expected_fields = {
    "id": Integer,
    "name": String,
    "kind": String,
    "category": String,
    "criteria": Text,
    "dag": JSON,
    "code": Text,
    "multi_turn": Boolean,
    "params": JSON,
    "threshold": Float,
    "is_active": Boolean,
    "created_at": DateTime,
    "updated_at": DateTime,
}

for field_name, expected_type in expected_fields.items():
    col = getattr(CustomMetric, field_name, None)
    assert_true(f"字段 '{field_name}' 存在", col is not None)
    if col is not None:
        actual_type = type(col.type)
        assert_true(
            f"字段 '{field_name}' 类型为 {expected_type.__name__}",
            isinstance(col.type, expected_type),
        )

# --- 测试3：主键与 autoincrement ---
print("\n[测试3] 主键与 autoincrement")
pk = CustomMetric.__table__.primary_key
assert_true("主键列存在", len(pk.columns) == 1)
assert_true("主键为 id", "id" in pk.columns)
id_col = pk.columns["id"]
assert_true("id 为 autoincrement", id_col.autoincrement is True)

# --- 测试4：实例创建与默认值 ---
print("\n[测试4] 实例创建与默认值")
cm = CustomMetric(name="测试指标", kind="geval", criteria="判断回答是否准确")
assert_eq("name", cm.name, "测试指标")
assert_eq("kind", cm.kind, "geval")
assert_eq("criteria", cm.criteria, "判断回答是否准确")
# SQLAlchemy 的 default 是 INSERT 时的数据库端默认值，不在 Python 实例化时填充
# 改为检查 Column.default.arg 验证默认值定义
from sqlalchemy import inspect as sa_inspect
mapper = sa_inspect(CustomMetric)
assert_eq("threshold Column default", mapper.columns["threshold"].default.arg, 0.5)
assert_eq("is_active Column default", mapper.columns["is_active"].default.arg, True)
assert_eq("multi_turn Column default", mapper.columns["multi_turn"].default.arg, False)
assert_true("dag 默认为 None", cm.dag is None)
assert_true("code 默认为 None", cm.code is None)
assert_true("params 默认为 None", cm.params is None)

# --- 测试5：三种 kind 模式的字段填充 ---
print("\n[测试5] 三种 kind 模式的字段填充")

# geval 模式
geval_cm = CustomMetric(
    name="安全合规检查",
    kind="geval",
    criteria="判断回答是否包含危险建议，给出 0-1 评分",
    params=["INPUT", "ACTUAL_OUTPUT"],
    threshold=0.3,
)
assert_eq("geval.kind", geval_cm.kind, "geval")
assert_true("geval.criteria 非空", "危险建议" in geval_cm.criteria)
assert_eq("geval.params", geval_cm.params, ["INPUT", "ACTUAL_OUTPUT"])

# dag 模式
dag_cm = CustomMetric(
    name="工具调用决策树",
    kind="dag",
    dag={
        "root": {
            "name": "是否调用了正确工具",
            "type": "judge",
            "instruction": "检查工具名称是否正确",
            "children": [
                {"name": "正确", "score": 1.0},
                {"name": "错误", "score": 0.0},
            ],
        }
    },
    threshold=0.5,
)
assert_eq("dag.kind", dag_cm.kind, "dag")
assert_true("dag.dag 非空", isinstance(dag_cm.dag, dict))
assert_true("dag.dag 有 root", "root" in dag_cm.dag)

# code 模式
code_cm = CustomMetric(
    name="长度检查",
    kind="code",
    code="score = min(1.0, len(tc.get('actual_output', '')) / 100)\nreason = f'长度得分 {score}'",
    threshold=0.6,
)
assert_eq("code.kind", code_cm.kind, "code")
assert_true("code.code 非空", "score" in code_cm.code)

# multi_turn 指标
multi_cm = CustomMetric(
    name="多轮完整性",
    kind="geval",
    criteria="判断多轮对话是否完整满足用户诉求",
    multi_turn=True,
    params=["ROLE", "CONTENT"],
)
assert_eq("multi_turn.multi_turn", multi_cm.multi_turn, True)

# --- 测试6：Base.metadata 包含新表 ---
print("\n[测试6] Base.metadata 包含 custom_metrics 表")
assert_true(
    "custom_metrics 在 metadata 中",
    "custom_metrics" in Base.metadata.tables,
)
table = Base.metadata.tables["custom_metrics"]
assert_eq("表列数", len(table.columns), 13)  # 022 迁移新增 category 列

# --- 测试7：回归 — 现有 ORM 模型不受影响 ---
print("\n[测试7] 回归 — 现有 ORM 模型不受影响")
from database import (
    Dataset, Agent, EvaluationTask, EvaluationResult,
    AgentRun, RunSpanNode, RunSemanticNode, RunMetric,
    CheckpointResult, RunComparison, RunDiff,
    Experiment, ExperimentSummary, AgentRunTraceRef,
    TraceSyncJob, EvaluationRound, EvaluationScript,
    ProcessCheckpointTemplate, ScenarioEvalTemplate,
    ScenarioEvalStep, ScenarioStepResult, ScenarioStepSpanRef,
    AgentAssetSnapshot, TraceOperationRule, LLMModel, EvaluationReport,
)

# 验证关键现有模型的表名未变
assert_eq("Dataset 表名", Dataset.__tablename__, "datasets")
assert_eq("EvaluationTask 表名", EvaluationTask.__tablename__, "evaluation_tasks")
assert_eq("EvaluationResult 表名", EvaluationResult.__tablename__, "evaluation_results")
assert_eq("LLMModel 表名", LLMModel.__tablename__, "llm_models")
assert_eq("EvaluationReport 表名", EvaluationReport.__tablename__, "evaluation_reports")
assert_eq("EvaluationScript 表名", EvaluationScript.__tablename__, "evaluation_scripts")

# 验证现有模型字段数未变（抽查 EvaluationTask 关键字段）
et_cols = {c.name for c in EvaluationTask.__table__.columns}
assert_true("EvaluationTask.selected_metrics 存在", "selected_metrics" in et_cols)
assert_true("EvaluationTask.custom_scripts 存在", "custom_scripts" in et_cols)
assert_true("EvaluationTask.llm_model 存在", "llm_model" in et_cols)
assert_true("EvaluationTask 有 custom_metric_ids（T3-2 新增）", "custom_metric_ids" in et_cols)

# 验证 metadata 中现有表仍存在
for tname in ["datasets", "agents", "evaluation_tasks", "evaluation_results", "llm_models"]:
    assert_true(f"表 '{tname}' 仍在 metadata 中", tname in Base.metadata.tables)

# --- 测试8：总表数 ---
print("\n[测试8] 总表数")
table_count = len(Base.metadata.tables)
print(f"  metadata 中共 {table_count} 张表")
assert_true("表数 >= 27（26 现有 + 1 新增）", table_count >= 27)

# ===== 结果汇总 =====
print("\n" + "=" * 60)
print(f"测试结果：{_passed} 通过，{_failed} 失败")
print("=" * 60)
sys.exit(0 if _failed == 0 else 1)
