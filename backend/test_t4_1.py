# -*- coding: utf-8 -*-
"""T4-1 测试用例：在线管线 DeepEval + 自定义指标分支

测试覆盖：
1. 新增函数存在性验证（_run_custom_metric / _run_code_metric_sync / _run_deepeval_custom_sync）
2. _persist_quality_evaluation_result 有 eval_method_override 参数
3. import 正确（DEEPEVAL_METRIC_KEYS / get_metric / CustomMetric）
4. code 档沙箱执行（确定性，无 LLM）
5. 异常处理（code 执行失败 → score=0.0 非阻塞）
6. 回归：原有函数签名不变（_judge_quality_metric_once / _persist_quality_evaluation_result）
"""
import sys
import os
import inspect
import asyncio

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
print("T4-1 测试：在线管线 DeepEval + 自定义指标分支")
print("=" * 60)

# --- 测试1：新增函数存在性 ---
print("\n[测试1] 新增函数存在性")
from services.evaluation_pipeline import (
    _run_custom_metric,
    _run_code_metric_sync,
    _run_deepeval_custom_sync,
)
assert_true("_run_custom_metric 存在", callable(_run_custom_metric))
assert_true("_run_code_metric_sync 存在", callable(_run_code_metric_sync))
assert_true("_run_deepeval_custom_sync 存在", callable(_run_deepeval_custom_sync))

# --- 测试2：_persist_quality_evaluation_result 有 eval_method_override 参数 ---
print("\n[测试2] eval_method_override 参数")
from services.evaluation_pipeline import _persist_quality_evaluation_result
sig = inspect.signature(_persist_quality_evaluation_result)
assert_true("eval_method_override 参数存在", "eval_method_override" in sig.parameters)
assert_true("eval_method_override 默认 None", sig.parameters["eval_method_override"].default is None)

# --- 测试3：import 正确 ---
print("\n[测试3] import 验证")
import services.evaluation_pipeline as ep
assert_true("DEEPEVAL_METRIC_KEYS 可访问", hasattr(ep, "DEEPEVAL_METRIC_KEYS"))
assert_true("get_metric 可访问", hasattr(ep, "get_metric"))
assert_true("CustomMetric 可访问", hasattr(ep, "CustomMetric"))
assert_true("DEEPEVAL_METRIC_KEYS 是 set", isinstance(ep.DEEPEVAL_METRIC_KEYS, set))

# --- 测试4：code 档沙箱执行（确定性，无 LLM/DeepEval） ---
print("\n[测试4] code 档沙箱执行")
cm_def_code = {
    "key": "custom_99",
    "name": "长度检查",
    "kind": "code",
    "code": "score = 1.0 if len(tc.get('actual_output', '')) > 10 else 0.0\nreason = '长度检查通过' if score > 0 else '回答过短'",
}
result = _run_code_metric_sync(cm_def_code, "你好", "这是一个足够长的回答用于测试", None, "期望答案")
assert_true("返回 dict", isinstance(result, dict))
assert_eq("score", result["score"], 1.0)
assert_eq("reason", result["reason"], "长度检查通过")
assert_true("duration_ms >= 0", result["duration_ms"] >= 0)

# code 档：短回答 → score=0.0
result_short = _run_code_metric_sync(cm_def_code, "你好", "短", None, "期望答案")
assert_eq("短回答 score", result_short["score"], 0.0)

# --- 测试5：异常处理（code 执行失败 → 抛 ValueError，由 _run_custom_metric 兜底） ---
print("\n[测试5] 异常处理 — code 执行失败")
cm_def_bad = {
    "key": "custom_98",
    "name": "错误代码",
    "kind": "code",
    "code": "score = 1 / 0  # 除零触发异常",
}
# _run_code_metric_sync 应抛出 ValueError（沙箱内代码报错）
try:
    _run_code_metric_sync(cm_def_bad, "你好", "回答", None, "期望")
    assert_true("_run_code_metric_sync 抛出 ValueError", False)
except ValueError as e:
    assert_true("_run_code_metric_sync 抛出 ValueError", True)
    assert_true("错误信息包含 '代码报错'", "代码报错" in str(e))

# _run_custom_metric 应捕获异常并返回 score=0.0
async def _test_code_exception():
    result = await _run_custom_metric(cm_def_bad, "你好", "回答", None, "期望", "model_key", "模型名")
    return result

result_bad = asyncio.run(_test_code_exception())
assert_eq("异常兜底 score", result_bad["score"], 0.0)
assert_true("异常兜底 reason 非空", len(result_bad["reason"]) > 0)

# --- 测试6：_run_custom_metric 异步包装 ---
print("\n[测试6] _run_custom_metric 异步调用")

async def _test_async():
    cm_def = {
        "key": "custom_97",
        "name": "测试code",
        "kind": "code",
        "code": "score = 0.5\nreason = '中等分数'",
    }
    result = await _run_custom_metric(cm_def, "问题", "回答", None, "期望", "model_key", "模型名")
    return result

result_async = asyncio.run(_test_async())
assert_eq("异步 score", result_async["score"], 0.5)
assert_eq("异步 reason", result_async["reason"], "中等分数")

# --- 测试7：_run_custom_metric 异常兜底 ---
print("\n[测试7] _run_custom_metric 异常兜底")
async def _test_exception():
    cm_def = {
        "key": "custom_96",
        "name": "自定义指标",
        "kind": "unknown_kind",  # 不支持的 kind
        "code": "",
    }
    result = await _run_custom_metric(cm_def, "问题", "回答", None, "期望", "model_key", "模型名")
    return result

result_exc = asyncio.run(_test_exception())
# unknown_kind 会进入 _run_deepeval_custom_sync，因为没有 deepeval 安装会 ImportError
# 最终被 _run_custom_metric 的 except 捕获
assert_eq("异常兜底 score", result_exc["score"], 0.0)
assert_true("异常兜底 reason 非空", len(result_exc["reason"]) > 0)

# --- 测试8：回归 — 原有函数签名不变 ---
print("\n[测试8] 回归 — 原有函数签名")
from services.evaluation_pipeline import _judge_quality_metric_once
sig_judge = inspect.signature(_judge_quality_metric_once)
# 原有参数应存在
expected_params = ["task_id", "question_id", "model_name", "metric_name", "threshold",
                    "question", "model_answer", "context", "ground_truth", "llm_model"]
for p in expected_params:
    assert_true(f"_judge_quality_metric_once 参数 '{p}' 存在", p in sig_judge.parameters)

# _persist_quality_evaluation_result 原有参数不变
expected_persist_params = ["task_id", "question_id", "metric_name", "threshold", "eval_result",
                           "use_script_eval", "script_id", "scripts_cache", "question",
                           "ground_truth", "model_answer", "agent_name", "llm_model", "llm_model_name"]
for p in expected_persist_params:
    assert_true(f"_persist_quality_evaluation_result 参数 '{p}' 存在", p in sig.parameters)

# ===== 结果汇总 =====
print("\n" + "=" * 60)
print(f"测试结果：{_passed} 通过，{_failed} 失败")
print("=" * 60)
sys.exit(0 if _failed == 0 else 1)
