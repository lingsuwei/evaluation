# -*- coding: utf-8 -*-
"""复用 DeepEval 4.x 基于 trace 的 Agentic 指标:步骤效率/计划遵循/计划质量。

这些指标读 DeepEval 私有 _trace_dict(嵌套 span),平台用 Langfuse 而非 DeepEval @observe,
故在此把执行数据(任务+输出+工具调用+思考步骤)桥接成 DeepEval 期望的 trace dict。
全部为非阻塞 MetricResult(与 judge 同语义);输入不足/失败则跳过,不污染结果。

源: agent_eval.evaluators.metrics.agentic（import 路径改为本子包相对引用）。
"""
from __future__ import annotations

import os
from typing import Any

from .types import MetricResult, ToolCallRecord
from .tool_metrics import to_deepeval_tools
from .deepeval_model import build_deepeval_model

# key -> (DeepEval 指标类名, 中文标签, 是否需要 agent 计划/思考)
_METRICS: dict[str, tuple[str, str, bool]] = {
    "step_efficiency": ("StepEfficiencyMetric", "步骤效率", False),
    "plan_adherence": ("PlanAdherenceMetric", "计划遵循", True),
    "plan_quality": ("PlanQualityMetric", "计划质量", True),
}
DEFAULT_METRICS = list(_METRICS.keys())


def build_trace_dict(
    task: str,
    output: str,
    tool_calls: list[ToolCallRecord],
    thoughts: list[str] | None = None,
) -> dict[str, Any]:
    """把一次运行桥接为 DeepEval 期望的嵌套 span 结构(name/input/output/children)。

    工具调用→子 span(inputParameters/output);思考步骤→reasoning 子 span(供计划类指标抽取计划)。
    """
    children: list[dict[str, Any]] = []
    if thoughts:
        children.append(
            {
                "name": "reasoning",
                "input": {"input": task},
                "output": "\n".join(str(t) for t in thoughts),
                "children": [],
            }
        )
    for call in tool_calls:
        children.append(
            {
                "name": call.name,
                "input": {"inputParameters": dict(call.arguments or {})},
                "output": call.output,
                "children": [],
            }
        )
    return {"name": "agent", "input": {"input": task}, "output": output, "children": children}


class AgenticMetricsEvaluator:
    """基于 DeepEval Agentic 指标的评估器。评测模型≠被测模型(用生成/辅助模型)。"""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str | None = None,
        api_key_env: str | None = None,
        temperature: float = 0.0,
        provider: str | None = None,
        threshold: float = 0.5,
        metrics: list[str] | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.provider = provider
        self.threshold = threshold
        self.metrics = [m for m in (metrics or DEFAULT_METRICS) if m in _METRICS]

    def evaluate(
        self,
        *,
        task_input: str,
        output: str,
        tool_calls: list[ToolCallRecord],
        thoughts: list[str] | None = None,
    ) -> list[MetricResult]:
        # 无执行步骤(工具调用)则没有可评的轨迹,直接跳过
        if not tool_calls:
            return []
        trace = build_trace_dict(task_input, output, tool_calls, thoughts)
        model = self._model()
        results: list[MetricResult] = []
        for key in self.metrics:
            result = self._run_one(key, trace, task_input, output, tool_calls, thoughts, model)
            if result is not None:
                results.append(result)
        return results

    def _run_one(self, key, trace, task_input, output, tool_calls, thoughts, model):
        import deepeval.metrics as M
        from deepeval.test_case import LLMTestCase

        cls_name, label, needs_plan = _METRICS[key]
        # 计划类指标需要 agent 暴露了思考/计划;否则跳过(避免无计划的噪声 0 分)
        if needs_plan and not thoughts:
            return None
        try:
            test_case = LLMTestCase(
                input=task_input,
                actual_output=output,
                tools_called=to_deepeval_tools(tool_calls),
            )
            test_case._trace_dict = trace
            metric = getattr(M, cls_name)(model=model, threshold=self.threshold)
            metric.measure(test_case)
            score = float(metric.score)
            reason = getattr(metric, "reason", None) or ""
            return MetricResult(f"agentic:{key}", score, score, reason[:2000])
        except Exception as exc:  # noqa: BLE001 - 非阻塞:失败跳过该指标
            return MetricResult(f"agentic:{key}", 0.0, None, f"{label}评估失败：{exc}")

    def _model(self):
        api_key = self.api_key or (os.getenv(self.api_key_env) if self.api_key_env else None)
        return build_deepeval_model(
            model=self.model,
            base_url=self.base_url,
            api_key=api_key,
            temperature=self.temperature,
            provider=self.provider or "local",
        )
