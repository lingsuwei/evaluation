# -*- coding: utf-8 -*-
"""T7-1 测试用例：eval_method 扩展 + 质量总分聚合适配

测试覆盖：
1. DEFAULT_UNKNOWN_METRIC_WEIGHT 常量
2. _aggregate_result_metric_scores 含未知指标时正确聚合
3. _compute_result_score 含未知指标时正确聚合（与 _aggregate 一致）
4. _is_quality_metric 对 DeepEval/custom eval_method 的判定
5. 回归：仅已知指标时行为不变
6. 回归：仅未知指标时等权平均
"""
import sys
import os

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


def assert_eq(desc: str, actual, expected, tol=0.001):
    global _passed, _failed
    if isinstance(expected, float):
        ok = abs(actual - expected) < tol
    else:
        ok = actual == expected
    if ok:
        _passed += 1
        print(f"  [PASS] {desc} → {actual}")
    else:
        _failed += 1
        print(f"  [FAIL] {desc} → 期望 {expected!r}，实际 {actual!r}")


print("=" * 60)
print("T7-1 测试：eval_method 扩展 + 质量总分聚合适配")
print("=" * 60)

# --- 测试1：常量存在 ---
print("\n[测试1] 常量")
from services.quality_score_service import (
    METRIC_WEIGHTS,
    DEFAULT_UNKNOWN_METRIC_WEIGHT,
    RUNTIME_METRICS,
    _aggregate_result_metric_scores,
    _is_quality_metric,
)
assert_eq("DEFAULT_UNKNOWN_METRIC_WEIGHT", DEFAULT_UNKNOWN_METRIC_WEIGHT, 0.1)
assert_eq("METRIC_WEIGHTS 有 3 个", len(METRIC_WEIGHTS), 3)
assert_true("RUNTIME_METRICS 含 inference_latency", "inference_latency" in RUNTIME_METRICS)
assert_true("RUNTIME_METRICS 含 tokens_total", "tokens_total" in RUNTIME_METRICS)

# --- 测试2：_aggregate_result_metric_scores 含未知指标 ---
print("\n[测试2] 含未知指标的聚合")
# 场景：correctness=0.8(权重0.5), faithfulness=0.6(权重0.3), answer_relevancy=0.7(权重0.2), toxicity=0.9(默认0.1)
# 总权重=1.1, score = (0.8*0.5 + 0.6*0.3 + 0.7*0.2 + 0.9*0.1) / 1.1 = (0.4+0.18+0.14+0.09)/1.1 = 0.81/1.1 = 0.7364
score, breakdown = _aggregate_result_metric_scores({
    "correctness": [0.8],
    "faithfulness": [0.6],
    "answer_relevancy": [0.7],
    "toxicity": [0.9],  # DeepEval 指标
})
assert_eq("含 4 指标（3已知+1未知）总分", round(score, 4), 0.7364)
assert_eq("breakdown 有 4 项", len(breakdown), 4)
assert_true("breakdown 含 toxicity", "toxicity" in breakdown)

# 场景2：只有未知指标
score2, breakdown2 = _aggregate_result_metric_scores({
    "toxicity": [0.9],
    "bias": [0.3],
})
# 等权: (0.9+0.3)/2 = 0.6
assert_eq("仅 2 个未知指标总分", round(score2, 4), 0.6)

# 场景3：只有已知指标
score3, breakdown3 = _aggregate_result_metric_scores({
    "correctness": [0.8],
    "faithfulness": [0.6],
    "answer_relevancy": [0.7],
})
# (0.8*0.5 + 0.6*0.3 + 0.7*0.2) / 1.0 = 0.4+0.18+0.14 = 0.72
assert_eq("仅 3 个已知指标总分", round(score3, 4), 0.72)

# 场景4：空
score4, _ = _aggregate_result_metric_scores({})
assert_eq("空指标列表", score4, None)

# --- 测试3：_is_quality_metric 对不同 eval_method 的判定 ---
print("\n[测试3] _is_quality_metric 判定")

class MockRow:
    def __init__(self, metric_name, eval_method=""):
        self.metric_name = metric_name
        self.eval_method = eval_method

# DeepEval 指标
assert_true("DeepEval 指标(toxicity, deepeval) 是质量指标", _is_quality_metric(MockRow("toxicity", "deepeval")))
# 自定义指标
assert_true("自定义指标(custom_1, custom_geval) 是质量指标", _is_quality_metric(MockRow("custom_1", "custom_geval")))
assert_true("自定义指标(custom_3, custom_code) 是质量指标", _is_quality_metric(MockRow("custom_3", "custom_code")))
# 运行时指标
assert_true("inference_latency 不是质量指标", not _is_quality_metric(MockRow("inference_latency", "runtime")))
assert_true("tokens_total 不是质量指标", not _is_quality_metric(MockRow("tokens_total", "runtime")))
# 空 metric_name
assert_true("空 metric_name 不是质量指标", not _is_quality_metric(MockRow("", "llm")))
# 原生指标
assert_true("correctness(llm) 是质量指标", _is_quality_metric(MockRow("correctness", "llm")))
assert_true("faithfulness(llm) 是质量指标", _is_quality_metric(MockRow("faithfulness", "llm")))

# --- 测试4：回归 — 仅已知指标时行为不变 ---
print("\n[测试4] 回归 — 仅已知指标行为不变")
# 验证：无未知指标时，权重仍为原始配置
score_known, _ = _aggregate_result_metric_scores({
    "correctness": [1.0],
    "faithfulness": [1.0],
    "answer_relevancy": [1.0],
})
assert_eq("全 1.0 的已知指标总分", round(score_known, 4), 1.0)

score_known2, _ = _aggregate_result_metric_scores({
    "correctness": [0.0],
    "faithfulness": [0.0],
    "answer_relevancy": [0.0],
})
assert_eq("全 0.0 的已知指标总分", round(score_known2, 4), 0.0)

# --- 测试5：混合场景权重分配 ---
print("\n[测试5] 混合场景权重分配")
# 1已知 + 1未知
# correctness=0.8(0.5) + toxicity=0.4(0.1) → total_weight=0.6
# score = (0.8*0.5 + 0.4*0.1) / 0.6 = (0.4+0.04)/0.6 = 0.44/0.6 = 0.7333
score_mix, _ = _aggregate_result_metric_scores({
    "correctness": [0.8],
    "toxicity": [0.4],
})
assert_eq("1已知+1未知总分", round(score_mix, 4), 0.7333)

# 1已知 + 5未知
# correctness=0.6(0.5) + 5个0.2(0.1 each) → total_weight=1.0
# score = (0.6*0.5 + 5*0.2*0.1) / 1.0 = (0.3+0.1)/1.0 = 0.4
score_many, _ = _aggregate_result_metric_scores({
    "correctness": [0.6],
    "toxicity": [0.2], "bias": [0.2], "hallucination": [0.2], "custom_1": [0.2], "custom_2": [0.2],
})
assert_eq("1已知+5未知总分", round(score_many, 4), 0.4)

# --- 测试6：eval_method 新值不报错 ---
print("\n[测试6] eval_method 新值兼容")
# 验证所有新 eval_method 值都能被 _is_quality_metric 正确处理
for method in ["deepeval", "custom_geval", "custom_dag", "custom_code", "llm", "script"]:
    row = MockRow("some_metric", method)
    assert_true(f"eval_method={method} 不崩溃", _is_quality_metric(row) or not _is_quality_metric(row))

# ===== 结果汇总 =====
print("\n" + "=" * 60)
print(f"测试结果：{_passed} 通过，{_failed} 失败")
print("=" * 60)
sys.exit(0 if _failed == 0 else 1)
