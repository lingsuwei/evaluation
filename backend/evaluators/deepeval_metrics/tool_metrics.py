# -*- coding: utf-8 -*-
"""复用 DeepEval 工具型指标的适配层（任务型 Agent）。

- ToolCorrectnessMetric：工具调用名称/顺序是否正确（确定性，model 仅占位、不真调 LLM）。
- ArgumentCorrectnessMetric：工具参数是否正确（LLM 判分）。

源: agent_eval.evaluators.metrics.tool_metrics（import 路径改为本子包相对引用）。
"""
from __future__ import annotations

import os

from .types import MetricResult, ToolCallRecord
from .deepeval_model import build_deepeval_model

TOOL_CORRECTNESS = "tool_correctness"
ARGUMENT_CORRECTNESS = "argument_correctness"


def to_deepeval_tools(tool_calls: list[ToolCallRecord]):
    """ToolCallRecord → DeepEval ToolCall（judge/task/tool 指标共享）。"""
    from deepeval.test_case import ToolCall

    return [
        ToolCall(name=call.name, input_parameters=dict(call.arguments or {}), output=call.output)
        for call in tool_calls
    ]


class ToolMetricsEvaluator:
    """基于 DeepEval 的工具调用正确性 + 参数正确性评估器。"""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str | None = None,
        api_key_env: str | None = None,
        temperature: float = 0.0,
        provider: str | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.provider = provider
        self._cached_model = None

    def evaluate(
        self,
        *,
        task_input: str,
        output: str,
        tool_calls: list[ToolCallRecord],
        expected_tool_names: list[str],
    ) -> list[MetricResult]:
        results = [self._tool_correctness(output, tool_calls, expected_tool_names)]
        results.append(self._argument_correctness(task_input, output, tool_calls))
        return results

    def _tool_correctness(self, output, tool_calls, expected_tool_names) -> MetricResult:
        try:
            from deepeval.metrics import ToolCorrectnessMetric
            from deepeval.test_case import LLMTestCase, ToolCall

            test_case = LLMTestCase(
                input="",
                actual_output=output,
                tools_called=to_deepeval_tools(tool_calls),
                expected_tools=[ToolCall(name=name) for name in expected_tool_names],
            )
            metric = ToolCorrectnessMetric(model=self._model(), should_consider_ordering=True)
            metric.measure(test_case)
            score = float(metric.score)
            return MetricResult(TOOL_CORRECTNESS, score, score, str(getattr(metric, "reason", "") or "")[:2000])
        except Exception as exc:  # noqa: BLE001
            return MetricResult(TOOL_CORRECTNESS, 0.0, None, f"tool_correctness 失败：{exc}")

    def _argument_correctness(self, task_input, output, tool_calls) -> MetricResult:
        try:
            from deepeval.metrics import ArgumentCorrectnessMetric
            from deepeval.test_case import LLMTestCase

            test_case = LLMTestCase(
                input=task_input, actual_output=output, tools_called=to_deepeval_tools(tool_calls)
            )
            metric = ArgumentCorrectnessMetric(model=self._model())
            metric.measure(test_case)
            score = float(metric.score)
            return MetricResult(ARGUMENT_CORRECTNESS, score, score, str(getattr(metric, "reason", "") or "")[:2000])
        except Exception as exc:  # noqa: BLE001
            return MetricResult(ARGUMENT_CORRECTNESS, 0.0, None, f"argument_correctness 失败：{exc}")

    def _model(self):
        if self._cached_model is not None:
            return self._cached_model
        api_key = self.api_key or (os.getenv(self.api_key_env) if self.api_key_env else None)
        self._cached_model = build_deepeval_model(
            model=self.model,
            base_url=self.base_url,
            api_key=api_key,
            temperature=self.temperature,
            provider=self.provider or "local",
        )
        return self._cached_model
