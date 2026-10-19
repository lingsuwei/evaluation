# -*- coding: utf-8 -*-
"""T3-2 测试用例：EvaluationTask 关联自定义指标

测试覆盖：
1. custom_metric_ids 字段存在性与类型
2. nullable=True（旧任务兼容）
3. 可读写 JSON ID 列表
4. 旧任务兼容（None → 应用层 or [] 兜底）
5. 回归：EvaluationTask 现有字段不受影响
"""
import sys
import os
import json

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
print("T3-2 测试：EvaluationTask.custom_metric_ids")
print("=" * 60)

# --- 测试1：字段存在性与类型 ---
print("\n[测试1] 字段存在性与类型")
from database import EvaluationTask
from sqlalchemy import Text

col = getattr(EvaluationTask, "custom_metric_ids", None)
assert_true("custom_metric_ids 字段存在", col is not None)
if col is not None:
    assert_true("类型为 Text", isinstance(col.type, Text))
    assert_true("nullable=True", col.nullable is True)

# --- 测试2：字段在正确位置（custom_scripts 之后） ---
print("\n[测试2] 字段位置")
cols = [c.name for c in EvaluationTask.__table__.columns]
idx_scripts = cols.index("custom_scripts")
idx_metric_ids = cols.index("custom_metric_ids")
idx_runtime = cols.index("runtime_metric_targets")
assert_true("custom_metric_ids 在 custom_scripts 之后", idx_metric_ids == idx_scripts + 1)
assert_true("custom_metric_ids 在 runtime_metric_targets 之前", idx_metric_ids < idx_runtime)

# --- 测试3：可读写 JSON ID 列表 ---
print("\n[测试3] 可读写 JSON ID 列表")
task = EvaluationTask(
    id="test-task-001",
    dataset_id="ds-001",
    selected_metrics='["faithfulness","toxicity"]',
    custom_metric_ids=json.dumps([1, 3, 5]),
)
assert_eq("写入 custom_metric_ids", task.custom_metric_ids, "[1, 3, 5]")

# 应用层读取模式
ids = json.loads(task.custom_metric_ids) if task.custom_metric_ids else []
assert_eq("读取解析 ID 列表", ids, [1, 3, 5])

# 空列表写入
task2 = EvaluationTask(id="test-task-002", custom_metric_ids=json.dumps([]))
ids2 = json.loads(task2.custom_metric_ids) if task2.custom_metric_ids else []
assert_eq("空列表写入读取", ids2, [])

# --- 测试4：旧任务兼容（None 兜底） ---
print("\n[测试4] 旧任务兼容（None 兜底）")
task_old = EvaluationTask(id="old-task-001")  # 不设置 custom_metric_ids
assert_true("旧任务 custom_metric_ids 为 None", task_old.custom_metric_ids is None)

# 应用层兜底模式：None → []
ids_old = json.loads(task_old.custom_metric_ids) if task_old.custom_metric_ids else []
assert_eq("旧任务 None 兜底为 []", ids_old, [])

# --- 测试5：回归 — EvaluationTask 现有字段不受影响 ---
print("\n[测试5] 回归 — EvaluationTask 现有字段不受影响")
expected_existing_fields = [
    "id", "dataset_id", "models", "status", "progress_current", "progress_total",
    "created_at", "completed_at", "error", "summary", "paused_at", "resume_from",
    "resume_agent_id", "run_count", "inference_rounds", "llm_model", "agent_id",
    "use_bm25", "use_doc_library", "ontology_package", "selected_metrics",
    "selected_indices", "custom_scripts", "runtime_metric_targets", "verdict_thresholds",
    "auto_scenario_score", "evaluation_mode", "process_eval_completed", "langfuse_source",
]
for fname in expected_existing_fields:
    assert_true(f"现有字段 '{fname}' 仍存在", fname in cols)

# 字段总数 = 现有 29 + 新增 1 = 30
assert_eq("EvaluationTask 字段总数", len(cols), 30)

# 关键字段抽样验证
assert_true("selected_metrics 仍为 Text", isinstance(EvaluationTask.__table__.columns["selected_metrics"].type, Text))
assert_true("custom_scripts 仍为 Text", isinstance(EvaluationTask.__table__.columns["custom_scripts"].type, Text))
assert_true("langfuse_source 仍为 JSON", not isinstance(EvaluationTask.__table__.columns["langfuse_source"].type, Text))

# --- 测试6：与 CustomMetric 的关联语义验证 ---
print("\n[测试6] 与 CustomMetric 的关联语义")
from database import CustomMetric

# 构造场景：2 个自定义指标，任务引用其中 1 个
cm1 = CustomMetric(name="安全检查", kind="geval", criteria="判断是否安全")
cm2 = CustomMetric(name="长度检查", kind="code", code="score = 1.0")
task_with_cm = EvaluationTask(
    id="task-with-custom",
    custom_metric_ids=json.dumps([1]),  # 只引用 cm1
)
linked_ids = json.loads(task_with_cm.custom_metric_ids) if task_with_cm.custom_metric_ids else []
assert_eq("任务引用了 1 个自定义指标", len(linked_ids), 1)
assert_eq("引用的 ID", linked_ids[0], 1)

# ===== 结果汇总 =====
print("\n" + "=" * 60)
print(f"测试结果：{_passed} 通过，{_failed} 失败")
print("=" * 60)
sys.exit(0 if _failed == 0 else 1)
