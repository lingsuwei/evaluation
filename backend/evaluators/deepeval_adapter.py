# -*- coding: utf-8 -*-
"""DeepEval 指标适配器——把 DeepEval 声明式指标适配到 onto-eval 的 BaseMetric 接口。

每次 evaluate() 调用：按 spec.kind 分流构造 LLMTestCase/ConversationalTestCase →
实例化 DeepEval 指标 → measure() → 转换为 EvaluationResult。
DeepEval 的 measure() 是同步调用（async_mode=False），通过 asyncio.to_thread 避免阻塞事件循环。

输入门控：spec.requires 中任一输入缺失 → 返回 score=None「已跳过」（不产 0 分误判）。
DeepEval 作为可选依赖：未安装时 evaluate() 返回 score=None 降级结果，不影响 onto-eval 现有指标。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from .base_metric import BaseMetric, EvaluationResult
from .llm_client import _LLM_CALL_SEMAPHORE


class DeepEvalMetricAdapter(BaseMetric):
    """把 DeepEval 声明式指标适配到 onto-eval 的 BaseMetric 接口。

    通过 registry.py 的 register_deepeval_metrics() 注册，每个 MetricSpec 对应一个适配器实例。
    签名扩展为 9 参(基础 4 参 + tool_calls/expected_tools/thoughts/retrieval_context/expected_output/turns)，
    新参为 keyword-only 且默认 None,向后兼容 pipeline 现有 4 参调用。
    """

    def __init__(
        self,
        spec: dict,
        model_config: dict,
        metric_params: dict | None = None,
    ) -> None:
        """
        Args:
            spec: MetricSpec 的字典形式（key, cls, extra, requires, params, label, kind）
            model_config: 评测模型配置 {model, base_url, api_key, temperature, provider}
            metric_params: 用户配置的指标参数（如 prompt_instructions, expected_schema 等）
        """
        self.name: str = spec.get("key", "deepeval")
        self._spec = spec
        self._model_config = model_config
        self._params = metric_params or {}
        self._cached_model = None

    async def evaluate(
        self,
        question: str,
        answer: str,
        context: Optional[str] = None,
        ground_truth: Optional[str] = None,
        *,
        tool_calls: Optional[list] = None,
        expected_tools: Optional[list[str]] = None,
        thoughts: Optional[list[str]] = None,
        retrieval_context: Optional[list[str]] = None,
        expected_output: Optional[str] = None,
        turns: Optional[list] = None,
        system_prompt: Optional[str] = None,
    ) -> EvaluationResult:
        """对单个问答执行 DeepEval 指标评测。

        基础 4 参(question/answer/context/ground_truth)向后兼容；新增 keyword-only 参
        供 Phase 3 pipeline 透传 Agent 推理过程数据(tool_calls/thoughts/turns/retrieval_context)。
        system_prompt 参数用于角色边界类指标(如 role_violation),将被测 Agent 的角色设定
        注入到 judge 的评分标准中。
        Phase 1 阶段 pipeline 仍传 4 参,需过程数据的指标自动「已跳过」(score=None)。

        Returns:
            EvaluationResult(score, reason, duration_ms)
            - score=None: 已跳过（缺输入或缺必填参数）
            - score=0.0: 评估失败（DeepEval 抛异常）
            - score=float: 实际评分(0-1)
        """
        t0 = time.time()
        try:
            # 在线程中执行同步的 DeepEval measure() 调用
            # 受全局信号量控制，与原生指标共享 8 并发上限
            async with _LLM_CALL_SEMAPHORE:
                result = await asyncio.to_thread(
                    self._evaluate_sync,
                    question=question,
                    answer=answer,
                    context=context,
                    ground_truth=ground_truth,
                    tool_calls=tool_calls,
                    expected_tools=expected_tools,
                    thoughts=thoughts,
                    retrieval_context=retrieval_context,
                    expected_output=expected_output,
                    turns=turns,
                    system_prompt=system_prompt,
                )
            return result
        except ImportError:
            return EvaluationResult(
                score=None,
                reason="deepeval 未安装，该指标不可用",
                duration_ms=int((time.time() - t0) * 1000),
            )
        except Exception as e:
            return EvaluationResult(
                score=0.0,
                reason=f"{self._spec.get('label', self.name)}评估失败：{e}",
                duration_ms=int((time.time() - t0) * 1000),
            )

    def _evaluate_sync(
        self,
        *,
        question: str,
        answer: str,
        context: Optional[str] = None,
        ground_truth: Optional[str] = None,
        tool_calls: Optional[list] = None,
        expected_tools: Optional[list[str]] = None,
        thoughts: Optional[list[str]] = None,
        retrieval_context: Optional[list[str]] = None,
        expected_output: Optional[str] = None,
        turns: Optional[list] = None,
        system_prompt: Optional[str] = None,
    ) -> EvaluationResult:
        """同步执行 DeepEval 指标评测（在线程池中调用）。

        按 spec.kind 分流构造 LLMTestCase/ConversationalTestCase；
        输入不满足 spec.requires 时返回 score=None「已跳过」。
        """
        # 门控逻辑不需要 deepeval,放最前(避免 deepeval 未装时门控失效,缺输入指标无法正确跳过)
        from .deepeval_metrics.deepeval_suite import _metric_kwargs, _REQUIRES_LABELS

        t0 = time.time()

        spec = self._spec_to_metricspec()
        kind = spec.kind or "single"

        # 1. 输入可用性字典（供 _satisfied 门控）
        avail = {
            "tools": bool(tool_calls),
            "expected_tools": bool(expected_tools),
            "trace": bool(tool_calls),
            "thoughts": bool(thoughts),
            "retrieval_context": bool(retrieval_context),
            "context": bool(context),
            "expected_output": bool(expected_output or ground_truth),
            "turns": bool(turns),
        }

        # 2. 输入门控:缺所需输入 → 已跳过(score=None,不产 0 分误判)
        if not self._satisfied(spec, avail):
            missing = [_REQUIRES_LABELS.get(r, r) for r in spec.requires if not avail.get(r, False)]
            reason = "已跳过：该用例缺少 " + "、".join(missing) if missing else "已跳过"
            return EvaluationResult(
                score=None,
                reason=reason,
                duration_ms=int((time.time() - t0) * 1000),
            )

        # 3. 必填用户参数检查(如 prompt_alignment 的 prompt_instructions)
        kwargs = _metric_kwargs(spec, self._params)
        if kwargs is None:
            need = "、".join(p.label for p in spec.params if p.required) or "参数"
            return EvaluationResult(
                score=None,
                reason=f"已跳过：需先配置参数（{need}）",
                duration_ms=int((time.time() - t0) * 1000),
            )

        # 3.5. conversational kind 专用门控:无 turns → 跳过(不需 deepeval,提前判定)
        # 避免单轮用例上的 conversational 指标因 deepeval 未装而误报「未安装」而非「跳过」
        if kind == "conversational" and not turns:
            return EvaluationResult(
                score=None,
                reason="已跳过：该用例不是多轮对话(无多轮回合)",
                duration_ms=int((time.time() - t0) * 1000),
            )

        # 4. 构造测试用例 + measure 需要 deepeval(门控通过后再 import,未装则抛 ImportError 走外层降级)
        from deepeval.test_case import (
            LLMTestCase,
            LLMTestCaseParams,
            ConversationalTestCase,
            Turn,
            ToolCall,
        )
        import deepeval.metrics as M

        # 按 kind 分流构造测试用例
        if kind == "conversational":
            tc = self._build_conversational_test_case(turns, answer)
            if tc is None:
                return EvaluationResult(
                    score=None,
                    reason="已跳过：该用例不是多轮对话(无多轮回合)",
                    duration_ms=int((time.time() - t0) * 1000),
                )
        else:
            tc = self._build_single_test_case(
                question, answer, context, ground_truth,
                tool_calls, expected_tools, retrieval_context, expected_output,
            )

        # 5. 实例化指标并 measure
        model = self._get_model()
        cls_name = spec.cls

        try:
            if cls_name == "GEval":
                # G-Eval:用 extra 里的 criteria + geval_params,拼接用户配置参数(如 domain)
                geval_params = spec.extra.get("geval_params", [])
                eval_params = [
                    getattr(LLMTestCaseParams, p)
                    for p in geval_params
                    if hasattr(LLMTestCaseParams, p)
                ]
                criteria = spec.extra.get("criteria", "")
                # 拼接用户配置参数(如 misuse 的 domain),让 LLM 在打分时参考
                for p in spec.params:
                    val = self._params.get(p.name)
                    if val:
                        criteria += f"\n\n{p.label}：{val}"
                # 角色边界类指标(如 role_violation):注入被测 Agent 的角色设定作为评分上下文
                if spec.needs_system_prompt and system_prompt:
                    criteria += f"\n\n【被测 Agent 角色设定】\n{system_prompt.strip()}"
                metric = M.GEval(
                    name=spec.label,
                    criteria=criteria + "\n\n（请用中文输出评分理由 reason。）",
                    evaluation_params=eval_params or [
                        LLMTestCaseParams.INPUT,
                        LLMTestCaseParams.ACTUAL_OUTPUT,
                    ],
                    model=model,
                    async_mode=False,
                )
            else:
                # 常规 DeepEval 指标:动态实例化
                # async_mode=False 串行调用,避免并发把评测模型打超时
                metric = getattr(M, cls_name)(
                    model=model,
                    async_mode=False,
                    **kwargs,
                )

            metric.measure(tc)
            score = float(metric.score)
            reason = str(getattr(metric, "reason", "") or "")[:2000]
            return EvaluationResult(
                score=score,
                reason=reason,
                duration_ms=int((time.time() - t0) * 1000),
            )
        except Exception as e:
            return EvaluationResult(
                score=0.0,
                reason=f"{spec.label}评估失败：{e}",
                duration_ms=int((time.time() - t0) * 1000),
            )

    # ---- 测试用例构造(参考 deepeval_suite.DeepEvalSuiteEvaluator) ----

    def _satisfied(self, spec, avail: dict[str, bool]) -> bool:
        """输入门控:spec.requires 中所有输入都可用才返回 True。"""
        return all(avail.get(req, False) for req in spec.requires)

    def _build_single_test_case(
        self,
        question: str,
        answer: str,
        context: Optional[str],
        ground_truth: Optional[str],
        tool_calls: Optional[list],
        expected_tools: Optional[list[str]],
        retrieval_context: Optional[list[str]],
        expected_output: Optional[str],
    ):
        """构造单轮 LLMTestCase,填全 DeepEval 所需字段。"""
        from deepeval.test_case import LLMTestCase, ToolCall

        from .deepeval_metrics.agentic import build_trace_dict
        from .deepeval_metrics.tool_metrics import to_deepeval_tools

        tc_kwargs: dict[str, Any] = {
            "input": question or "",
            "actual_output": answer or "",
        }
        # context(给定事实,给 hallucination 用)与 retrieval_context(检索片段,给 RAG 用)语义不同,分开传
        if context:
            tc_kwargs["context"] = [context]
        if retrieval_context:
            tc_kwargs["retrieval_context"] = list(retrieval_context)
        # expected_output 优先用专用字段,兜底用 ground_truth(onto-eval 同源)
        eo = expected_output or ground_truth
        if eo:
            tc_kwargs["expected_output"] = eo
        if tool_calls:
            tc_kwargs["tools_called"] = to_deepeval_tools(tool_calls)
        if expected_tools:
            tc_kwargs["expected_tools"] = [ToolCall(name=n) for n in expected_tools]

        tc = LLMTestCase(**tc_kwargs)
        # step_efficiency/plan_adherence/plan_quality 等推理类指标内部读 _trace_dict
        if tool_calls:
            tc._trace_dict = build_trace_dict(question or "", answer or "", tool_calls, None)
        return tc

    def _build_conversational_test_case(self, turns: Optional[list], answer: str):
        """构造多轮 ConversationalTestCase;无 user 回合返回 None(触发跳过)。"""
        from deepeval.test_case import ConversationalTestCase, Turn

        deepeval_turns = [
            Turn(role=t.role, content=t.content)
            for t in (turns or [])
            if getattr(t, "role", "") in ("user", "assistant") and getattr(t, "content", "")
        ]
        if not any(t.role == "user" for t in deepeval_turns):
            return None
        if answer:
            deepeval_turns.append(Turn(role="assistant", content=answer))
        try:
            return ConversationalTestCase(turns=deepeval_turns)
        except Exception:  # noqa: BLE001
            return None

    def _get_model(self):
        """懒加载 DeepEval 模型实例（缓存复用）。"""
        if self._cached_model is not None:
            return self._cached_model
        from .deepeval_metrics.deepeval_model import build_deepeval_model

        self._cached_model = build_deepeval_model(
            model=self._model_config.get("model", ""),
            base_url=self._model_config.get("base_url", ""),
            api_key=self._model_config.get("api_key"),
            temperature=self._model_config.get("temperature", 0.0),
            provider=self._model_config.get("provider"),
        )
        return self._cached_model

    def _spec_to_metricspec(self):
        """把字典形式的 spec 转回 MetricSpec 对象（供 _metric_kwargs 使用）。

        _metric_kwargs 接收 MetricSpec 对象，这里做一个轻量适配。
        """
        from .deepeval_metrics.deepeval_suite import MetricSpec, ParamSpec

        params = tuple(
            ParamSpec(
                name=p.get("name", ""),
                label=p.get("label", ""),
                type=p.get("type", "text"),
                required=p.get("required", True),
                placeholder=p.get("placeholder", ""),
            )
            for p in self._spec.get("params", [])
        )
        return MetricSpec(
            key=self._spec.get("key", ""),
            label=self._spec.get("label", ""),
            category=self._spec.get("category", ""),
            cls=self._spec.get("cls", ""),
            kind=self._spec.get("kind", "single"),
            requires=tuple(self._spec.get("requires", ())),
            extra=self._spec.get("extra", {}),
            params=params,
            note=self._spec.get("note", ""),
            lower_is_better=self._spec.get("lower_is_better", False),
            gate_threshold=self._spec.get("gate_threshold", 0.5),
        )
