# -*- coding: utf-8 -*-
"""T4-2 测试用例：离线管线 DeepEval + 自定义指标分支

通过源码结构验证离线管线的改动，不依赖完整运行环境。

测试覆盖：
1. run_offline_evaluation_pipeline 函数存在
2. 离线管线有 DeepEval 指标加载逻辑
3. 离线管线有自定义指标加载逻辑
4. 离线管线指标循环有三分支（DeepEval/自定义/原有）
5. 离线管线用 continue（非 return True）跳出分支
6. 离线管线 eval_method_override 调用正确
7. 回归：离线管线原有逻辑（resolve_offline_metrics_to_run）不变
8. 在线+离线两管线的 _run_custom_metric 调用一致
"""
import sys
import os
import inspect

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
print("T4-2 测试：离线管线 DeepEval + 自定义指标分支")
print("=" * 60)

# --- 测试1：run_offline_evaluation_pipeline 函数存在 ---
print("\n[测试1] 函数存在性")
from services.evaluation_pipeline import run_offline_evaluation_pipeline
assert_true("run_offline_evaluation_pipeline 存在", callable(run_offline_evaluation_pipeline))
assert_true("是协程函数", inspect.iscoroutinefunction(run_offline_evaluation_pipeline))

# --- 测试2：源码结构验证 ---
print("\n[测试2] 源码结构验证")
import services.evaluation_pipeline as ep
src = inspect.getsource(ep)

# 离线管线的 DeepEval 分支
assert_true("离线管线有 DeepEval 分支注释", "# === 分支1：DeepEval 指标 ===" in src)
# 统计出现次数（在线+离线各1次 = 2次）
deepeval_branch_count = src.count("# === 分支1：DeepEval 指标 ===")
assert_eq("DeepEval 分支出现次数（在线+离线）", deepeval_branch_count, 2)

# 离线管线的自定义分支
assert_true("离线管线有自定义指标分支注释", "# === 分支2：自定义指标 ===" in src)
custom_branch_count = src.count("# === 分支2：自定义指标 ===")
assert_eq("自定义分支出现次数（在线+离线）", custom_branch_count, 2)

# 离线管线的原有指标分支
assert_true("离线管线有原有指标分支注释", "# === 分支3：原有指标" in src)
original_branch_count = src.count("# === 分支3：原有指标")
assert_eq("原有指标分支出现次数（在线+离线）", original_branch_count, 2)

# --- 测试3：离线管线用 continue 跳出分支 ---
print("\n[测试3] 离线管线用 continue 跳出分支")
# 在线管线用 return True（在 _judge_one 闭包中），离线管线用 continue（在 for 循环中）
# 检查 continue 在 judge_done + continue 的上下文中出现（离线管线特有模式）
continue_after_judge_done = src.count('phase="judge_done",')
# 在线+离线各有2个分支 = 4次 judge_done（但在线用 return True，离线用 continue）
assert_true("judge_done 出现至少 4 次（在线+离线各2）", continue_after_judge_done >= 4)
# 离线管线的分支用 continue
continue_count = src.count("continue")
assert_true("continue 出现至少 2 次（离线管线2个分支）", continue_count >= 2)

# --- 测试4：离线管线有自定义指标加载逻辑 ---
print("\n[测试4] 自定义指标加载逻辑")
# custom_metric_defs 在离线管线中也出现（在线+离线各1次定义）
custom_defs_count = src.count("custom_metric_defs: list[dict] = []")
assert_eq("custom_metric_defs 定义次数（在线+离线）", custom_defs_count, 2)

# --- 测试5：eval_method_override 调用 ---
print("\n[测试5] eval_method_override 调用")
# DeepEval 分支：eval_method_override="deepeval"（在线+离线各1次 = 2次）
deepeval_override_count = src.count('eval_method_override="deepeval"')
assert_eq('eval_method_override="deepeval" 调用次数', deepeval_override_count, 2)

# 自定义分支：eval_method_override=eval_tag（在线+离线各1次 = 2次）
custom_override_count = src.count("eval_method_override=eval_tag")
assert_eq("eval_method_override=eval_tag 调用次数", custom_override_count, 2)

# --- 测试6：_run_custom_metric 在两管线中都被调用 ---
print("\n[测试6] _run_custom_metric 调用一致性")
run_custom_count = src.count("await _run_custom_metric(")
assert_eq("_run_custom_metric 调用次数（在线+离线）", run_custom_count, 2)

# --- 测试7：回归 — 原有函数不变 ---
print("\n[测试7] 回归 — 原有函数/结构不变")
from services.evaluation_pipeline import (
    _judge_quality_metric_once,
    _persist_quality_evaluation_result,
    _run_custom_metric,
    _run_code_metric_sync,
    _run_deepeval_custom_sync,
)
assert_true("_judge_quality_metric_once 仍可调用", callable(_judge_quality_metric_once))
assert_true("_persist_quality_evaluation_result 仍可调用", callable(_persist_quality_evaluation_result))
assert_true("_run_custom_metric 仍可调用", callable(_run_custom_metric))
assert_true("_run_code_metric_sync 仍可调用", callable(_run_code_metric_sync))
assert_true("_run_deepeval_custom_sync 仍可调用", callable(_run_deepeval_custom_sync))

# resolve_offline_metrics_to_run 仍被调用
from services.evaluation_mode import resolve_offline_metrics_to_run
assert_true("resolve_offline_metrics_to_run 可导入", callable(resolve_offline_metrics_to_run))
assert_true("源码中仍调用 resolve_offline_metrics_to_run", "resolve_offline_metrics_to_run(" in src)

# --- 测试8：离线管线参数完整性 ---
print("\n[测试8] 离线管线参数完整性")
sig = inspect.signature(run_offline_evaluation_pipeline)
expected_params = ["task_id", "selected_metrics"]
for p in expected_params:
    assert_true(f"参数 '{p}' 存在", p in sig.parameters)

# ===== 结果汇总 =====
print("\n" + "=" * 60)
print(f"测试结果：{_passed} 通过，{_failed} 失败")
print("=" * 60)
sys.exit(0 if _failed == 0 else 1)
