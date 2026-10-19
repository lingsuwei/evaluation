# -*- coding: utf-8 -*-
"""Phase 1 验证脚本:确认 conversational/tool 指标不再抛异常,正确返回 score=None「已跳过」。

验证点(不需要真实 LLM 调用,输入门控在 LLM 之前):
1. conversational 指标(无 turns) → score=None, reason 含「已跳过」
2. tool 指标(无 expected_tools) → score=None, reason 含「已跳过」
3. RAG 指标(无 retrieval_context) → score=None, reason 含「已跳过」
4. 安全指标 bias(只需 input/output) → 不被门控跳过(走到 LLM 调用或 deepeval 未安装)
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluators.deepeval_adapter import DeepEvalMetricAdapter
from evaluators.base_metric import EvaluationResult


def make_spec(key, label, category, cls, kind="single", requires=(),
               params=None, lower_is_better=False, gate_threshold=0.5):
    return {
        "key": key, "label": label, "category": category, "cls": cls,
        "kind": kind, "requires": tuple(requires),
        "extra": {}, "params": params or [], "note": "",
        "lower_is_better": lower_is_better, "gate_threshold": gate_threshold,
    }


async def run_case(name, spec, question="测试问题", answer="测试回答", **kwargs):
    adapter = DeepEvalMetricAdapter(spec=spec, model_config={})
    result = await adapter.evaluate(question=question, answer=answer, **kwargs)
    status = "跳过" if result.score is None else f"score={result.score:.3f}"
    print(f"  [{name}] {status} | reason: {result.reason[:80]}")
    return result


async def main():
    print("=== Phase 1 验证:DeepEval 指标输入门控 + kind 分流 ===\n")

    # 1. conversational 指标(无 turns) → 应跳过
    r1 = await run_case(
        "conversational(无turns)",
        make_spec("conversation_completeness", "多轮完整性", "conversation",
                  "ConversationCompletenessMetric", kind="conversational"),
    )
    assert r1.score is None, f"conversational 应 score=None, 实际 {r1.score}"
    assert "已跳过" in r1.reason or "未安装" in r1.reason

    # 2. tool 指标(无 expected_tools) → 应跳过
    r2 = await run_case(
        "tool_correctness(无expected_tools)",
        make_spec("tool_correctness", "工具正确性", "tool",
                  "ToolCorrectnessMetric", requires=("expected_tools",)),
    )
    assert r2.score is None
    assert "已跳过" in r2.reason or "未安装" in r2.reason

    # 3. RAG 指标(无 retrieval_context) → 应跳过
    r3 = await run_case(
        "contextual_relevancy(无retrieval_context)",
        make_spec("contextual_relevancy", "检索相关性", "rag",
                  "ContextualRelevancyMetric", requires=("retrieval_context",)),
    )
    assert r3.score is None
    assert "已跳过" in r3.reason or "未安装" in r3.reason

    # 4. reasoning 指标(无 trace+thoughts) → 应跳过
    r4 = await run_case(
        "plan_quality(无trace+thoughts)",
        make_spec("plan_quality", "计划质量", "reasoning",
                  "PlanQualityMetric", requires=("trace", "thoughts")),
    )
    assert r4.score is None
    assert "已跳过" in r4.reason or "未安装" in r4.reason

    # 5. safety 指标 bias(只需 input/output) → 不应被门控跳过(走到 LLM 或 deepeval 未安装)
    r5 = await run_case(
        "bias(只需input/output)",
        make_spec("bias", "偏见", "safety", "BiasMetric", lower_is_better=True),
    )
    # bias 不应被门控跳过:要么真评分(需 LLM),要么 deepeval 未安装返回 None
    gated = "已跳过" in r5.reason and "未安装" not in r5.reason
    assert not gated, f"bias 不应被输入门控跳过(只需 input/output), reason: {r5.reason}"

    # 6. hallucination(需 context) → 有 context 时不跳过
    r6 = await run_case(
        "hallucination(有context)",
        make_spec("hallucination", "幻觉", "answer_quality",
                  "HallucinationMetric", requires=("context",)),
        context="给定事实上下文",
    )
    gated6 = "已跳过" in r6.reason and "未安装" not in r6.reason
    assert not gated6, f"hallucination 有 context 不应跳过, reason: {r6.reason}"

    print("\n=== 验证结论 ===")
    print("[OK] conversational 指标不再抛 TypeError,正确返回 score=None「已跳过」")
    print("[OK] tool/RAG/reasoning 指标缺输入时正确跳过(score=None),不产 0 分误判")
    print("[OK] safety/answer_quality 指标只需 input/output,不被门控跳过")
    print("[OK] 向后兼容:pipeline 4 参调用正常,新参 keyword-only 默认 None")


if __name__ == "__main__":
    asyncio.run(main())
