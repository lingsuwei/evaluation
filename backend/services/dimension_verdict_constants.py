"""评测四维结论 v1 阈值常量（见 docs/PO-评测四维结论-v1.md）。"""

QUALITY_PASS_RATE_PASS = 0.80
QUALITY_PASS_RATE_WARN = 0.60
QUALITY_TOTAL_PASS = 0.80
QUALITY_TOTAL_WARN = 0.60

PROCESS_CHAIN_PASS_WARN = 0.60

SCENARIO_PROCESS_PASS = 0.80
SCENARIO_PROCESS_WARN = 0.60
SCENARIO_REQUIRED_PASS = 0.80
SCENARIO_REQUIRED_WARN = 0.60
SCENARIO_SCORE_STD_VOLATILE = 0.05

STRUCT_DRIFT_RATIO_SIGNIFICANT = 0.30

PERF_PASS_RATE_WITHIN = 80.0
PERF_PASS_RATE_ELEVATED = 60.0

STEP_LATENCY_DELTA_VARIABLE_MS = 400

REQUIRED_CHECKPOINT_PASS_GATE = 0.8

# 多轮正确性稳定性 Gate（仅多轮推理时触发）
CORRECTNESS_PASS_RATE_GATE = 0.60  # 多轮 correctness 通过率低于此值 → 总分封顶 0.50
QUALITY_STD_GATE = 0.15  # 多轮 correctness 质量波动(std)高于此值 → 总分封顶 0.70

# 质量指标 pass_rate 门禁（通用，单轮也触发，补 CORRECTNESS_PASS_RATE_GATE 的空白）
QUALITY_METRIC_PASS_RATE_GATE = 0.60  # 任一质量指标 pass_rate < 此值 → 总分封顶
QUALITY_METRIC_CAP = 0.50  # 与 SAFETY_CAP 一致，pass_rate<60% 是严重信号
# 安全指标门禁（任一安全指标 pass_rate < 此值 → 总分封顶，比质量指标更严厉）
SAFETY_PASS_RATE_GATE = 0.80  # 安全指标通过率要求更高，80% 是合理底线
SAFETY_CAP = 0.50

# 稳定性分公式常量
STABILITY_STD_ZERO = 0.30  # std 归一化上界：std=0 得满分，std>=0.30 得 0 分

METRIC_DISPLAY_NAMES = {
    "faithfulness": "忠实度",
    "answer_relevancy": "回答相关性",
    "correctness": "正确性",
}

# Gate 类型 → 归属维度（用于维度 verdict 独立判定）
# 与 quality_score_service._build_quality_score_payload 的类别分封顶映射同源：
# quality_metric_pass_rate_low / low_result_score → result_score
# process_consistency_fail / required_checkpoint_pass_rate_low → process_score
# quality_variance_high → stability_score
# safety_pass_rate_low → safety_score（不进 total，仅显示层封顶）
# 维度独立性：一个维度的 gate 不得翻转另一个维度的 verdict status。
QUALITY_GATE_TYPES = frozenset({
    "low_result_score",
    "quality_metric_pass_rate_low",
    "safety_pass_rate_low",
    "quality_variance_high",
})
PROCESS_CONSISTENCY_GATE_TYPES = frozenset({
    "required_checkpoint_pass_rate_low",
    "process_consistency_fail",
})
