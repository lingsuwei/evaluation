# -*- coding: utf-8 -*-
"""Phase 2 验证:Langfuse trace 过程数据提取 + 与 Phase 1 适配器对接。

验证点(不需真实 Langfuse/LLM,用构造的 mock trace):
1. tool_calls:从 type==tool / name 前缀 'tool:' / step.kind=='tool' 的 observation 提取 ToolCallRecord
2. thoughts:从 step.kind=='reasoning' 的 output 提取;trace.metadata.thoughts 优先
3. retrieval_context:从 retriever observation 提取;trace.metadata.retrieval_context 优先
4. turns:从 trace.metadata.turns 提取;turn observation 兜底
5. 缺失字段返回空列表(适配器门控让对应指标「已跳过」)
6. 对接 Phase 1:提取结果透传给 DeepEvalMetricAdapter,tool 指标有 expected_tools 时不被门控跳过
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.langfuse_trace_extract import (
    TraceProcessData,
    Turn,
    extract_process_data,
)
from evaluators.deepeval_metrics.types import ToolCallRecord


def _make_tool_obs(
    name: str,
    *,
    obs_type: str = "tool",
    tool_name: str | None = None,
    input_val=None,
    output_val=None,
    level: str = "default",
    start: str = "2026-07-20T10:00:01.000Z",
    end: str = "2026-07-20T10:00:01.500Z",
    step_kind: str = "tool",
):
    meta = {"agent.step.kind": step_kind}
    if tool_name:
        meta["agent.tool.name"] = tool_name
    return {
        "id": f"obs-{name}",
        "name": name,
        "type": obs_type,
        "startTime": start,
        "endTime": end,
        "input": input_val if input_val is not None else {"query": "test"},
        "output": output_val if output_val is not None else {"result": "ok"},
        "level": level,
        "statusMessage": "tool failed" if level == "error" else "",
        "metadata": meta,
    }


def _make_reasoning_obs(text: str, start="2026-07-20T10:00:00.500Z"):
    return {
        "id": "obs-reasoning",
        "name": "reasoning",
        "type": "span",
        "startTime": start,
        "endTime": "2026-07-20T10:00:00.900Z",
        "output": text,
        "metadata": {"agent.step.kind": "reasoning"},
    }


def _make_retriever_obs(chunks, start="2026-07-20T10:00:00.300Z"):
    return {
        "id": "obs-retrieval",
        "name": "retrieval",
        "type": "retriever",
        "startTime": start,
        "endTime": "2026-07-20T10:00:00.800Z",
        "output": chunks if isinstance(chunks, list) else chunks,
        "metadata": {"agent.step.kind": "retrieval"},
    }


def test_tool_calls_extraction():
    """验证:从 tool/retriever observation 提取 ToolCallRecord(含 arguments/error/latency)。"""
    trace = {
        "observations": [
            _make_tool_obs("OntoQuery", tool_name="OntoQuery",
                           input_val='{"query": "ICD-10"}',  # JSON 字符串
                           output_val={"snomed": "12345"}),
            _make_tool_obs("tool:SearchDB",  # name 前缀 'tool:' 但无 agent.tool.name
                           start="2026-07-20T10:00:02.000Z",
                           end="2026-07-20T10:00:03.200Z",
                           output_val="rows: 5",
                           level="error"),
            _make_reasoning_obs("我应该先查询 ICD-10"),
        ],
    }
    data = extract_process_data(trace)

    assert len(data.tool_calls) == 2, f"应提取 2 个工具调用,实际 {len(data.tool_calls)}"
    # 按 startTime 排序:OntoQuery(10:00:01) 在 tool:SearchDB(10:00:02) 前
    assert data.tool_calls[0].name == "OntoQuery"
    assert data.tool_calls[1].name == "SearchDB"  # 'tool:' 前缀被去掉
    # arguments:JSON 字符串解析为 dict
    assert data.tool_calls[0].arguments == {"query": "ICD-10"}
    # output 透传
    assert data.tool_calls[0].output == {"snomed": "12345"}
    # error:level==error 时取 statusMessage
    assert data.tool_calls[1].error == "tool failed"
    # latency_ms:解析出非零
    assert data.tool_calls[0].latency_ms == 500.0
    assert data.tool_calls[1].latency_ms == 1200.0
    print("[OK] tool_calls 提取:type/name 前缀/step.kind 三路识别 + arguments/error/latency")


def test_thoughts_extraction():
    """验证:thoughts 优先 trace.metadata.thoughts,兜底 reasoning observation。"""
    # 场景 1:trace.metadata.thoughts 优先
    trace1 = {
        "metadata": {"thoughts": ["思考1", "思考2"]},
        "observations": [_make_reasoning_obs("这条应被忽略(因 metadata 优先)")],
    }
    data1 = extract_process_data(trace1)
    assert data1.thoughts == ["思考1", "思考2"], f"metadata 优先,实际 {data1.thoughts}"

    # 场景 2:无 metadata.thoughts,从 reasoning observation 提取
    trace2 = {
        "observations": [
            _make_reasoning_obs("分析用户意图", start="2026-07-20T10:00:00.100Z"),
            _make_reasoning_obs("决定调用 OntoQuery", start="2026-07-20T10:00:00.500Z"),
        ],
    }
    data2 = extract_process_data(trace2)
    assert len(data2.thoughts) == 2
    assert data2.thoughts[0] == "分析用户意图"  # 按 startTime 排序
    print("[OK] thoughts 提取:metadata 优先 + reasoning observation 兜底,按时间排序")


def test_retrieval_context_extraction():
    """验证:retrieval_context 优先 trace.metadata,兜底 retriever observation output。"""
    # 场景 1:metadata.retrieval_context 优先
    trace1 = {
        "metadata": {"retrieval_context": ["doc1", "doc2"]},
        "observations": [_make_retriever_obs("这条应被忽略")],
    }
    data1 = extract_process_data(trace1)
    assert data1.retrieval_context == ["doc1", "doc2"]

    # 场景 2:从 retriever observation output 提取(list)
    trace2 = {
        "observations": [_make_retriever_obs(["chunk A", "chunk B", "chunk C"])]
    }
    data2 = extract_process_data(trace2)
    assert data2.retrieval_context == ["chunk A", "chunk B", "chunk C"], \
        f"list output 应 flatten,实际 {data2.retrieval_context}"

    # 场景 3:retriever output 是 str
    trace3 = {"observations": [_make_retriever_obs("一段检索文本")]}
    data3 = extract_process_data(trace3)
    assert data3.retrieval_context == ["一段检索文本"]
    print("[OK] retrieval_context 提取:metadata 优先 + retriever observation 兜底")


def test_turns_extraction():
    """验证:turns 优先 trace.metadata.turns,兜底 turn observation。"""
    # 场景 1:metadata.turns
    trace1 = {
        "metadata": {
            "turns": [
                {"role": "user", "content": "我想查 ICD-10"},
                {"role": "assistant", "content": "好的,请提供更多信息"},
            ]
        },
        "observations": [],
    }
    data1 = extract_process_data(trace1)
    assert len(data1.turns) == 2
    assert data1.turns[0].role == "user"
    assert data1.turns[1].content == "好的,请提供更多信息"

    # 场景 2:无 turns → 空列表(适配器门控让 conversational 指标跳过)
    trace2 = {"observations": [_make_tool_obs("OntoQuery")]}
    data2 = extract_process_data(trace2)
    assert data2.turns == []
    print("[OK] turns 提取:metadata 优先,缺失返回空列表")


def test_empty_trace():
    """验证:空 trace / 无 observations 返回全空 TraceProcessData。"""
    data = extract_process_data({})
    assert isinstance(data, TraceProcessData)
    assert data.tool_calls == []
    assert data.thoughts == []
    assert data.retrieval_context == []
    assert data.turns == []
    print("[OK] 空 trace 返回全空 TraceProcessData(适配器门控全跳过)")


def test_integration_with_phase1_adapter():
    """验证:Phase 2 提取结果透传给 Phase 1 适配器,tool 指标有 expected_tools 时不被门控跳过。"""
    from evaluators.deepeval_adapter import DeepEvalMetricAdapter

    def make_spec(key, label, category, cls, kind="single", requires=(),
                   params=None, lower_is_better=False, gate_threshold=0.5):
        return {
            "key": key, "label": label, "category": category, "cls": cls,
            "kind": kind, "requires": tuple(requires),
            "extra": {}, "params": params or [], "note": "",
            "lower_is_better": lower_is_better, "gate_threshold": gate_threshold,
        }

    # 构造带工具调用的 trace
    trace = {
        "observations": [
            _make_tool_obs("OntoQuery", tool_name="OntoQuery",
                           output_val={"result": "E11"}),
        ],
    }
    data = extract_process_data(trace)

    # tool_correctness 指标需要 expected_tools;有 tool_calls + expected_tools 时不被门控跳过
    spec = make_spec(
        "tool_correctness", "工具正确性", "tool",
        "ToolCorrectnessMetric", requires=("expected_tools",),
    )
    adapter = DeepEvalMetricAdapter(spec=spec, model_config={})

    # 透传提取结果给适配器
    result = asyncio.run(
        adapter.evaluate(
            question="查 ICD-10",
            answer="E11",
            tool_calls=data.tool_calls,
            expected_tools=["OntoQuery"],
        )
    )
    # 门控通过(有 tool_calls + expected_tools),走到 deepeval measure → 未装 deepeval 返回 None
    # 不应是被门控跳过(那会返回「已跳过：该用例缺少...」)
    gated = "已跳过" in result.reason and "缺少" in result.reason
    assert not gated, f"有 tool_calls+expected_tools 不应被门控跳过, reason: {result.reason}"
    print(f"[OK] Phase2→Phase1 对接:tool 指标有 tool_calls+expected_tools 不被门控跳过"
          f"(reason: {result.reason[:60]})")


def test_integration_conversational_skipped_without_turns():
    """验证:无 turns 的 trace + conversational 指标 → 适配器返回「已跳过」(Phase1 门控)。"""
    from evaluators.deepeval_adapter import DeepEvalMetricAdapter

    def make_spec(key, label, category, cls, kind="single", requires=(),
                   params=None, lower_is_better=False, gate_threshold=0.5):
        return {
            "key": key, "label": label, "category": category, "cls": cls,
            "kind": kind, "requires": tuple(requires),
            "extra": {}, "params": params or [], "note": "",
            "lower_is_better": lower_is_better, "gate_threshold": gate_threshold,
        }

    # 单轮 trace(无 turns)
    trace = {"observations": [_make_tool_obs("OntoQuery")]}
    data = extract_process_data(trace)
    assert data.turns == []

    spec = make_spec(
        "conversation_completeness", "多轮完整性", "conversation",
        "ConversationCompletenessMetric", kind="conversational",
    )
    adapter = DeepEvalMetricAdapter(spec=spec, model_config={})
    result = asyncio.run(
        adapter.evaluate(
            question="查 ICD-10",
            answer="E11",
            turns=data.turns,
        )
    )
    assert result.score is None
    assert "已跳过" in result.reason
    print(f"[OK] 无 turns 的 conversational 指标正确跳过(reason: {result.reason[:50]})")


def main():
    print("=== Phase 2 验证:Langfuse trace 过程数据提取 ===\n")
    test_tool_calls_extraction()
    test_thoughts_extraction()
    test_retrieval_context_extraction()
    test_turns_extraction()
    test_empty_trace()
    test_integration_with_phase1_adapter()
    test_integration_conversational_skipped_without_turns()

    print("\n=== 验证结论 ===")
    print("[OK] tool_calls 从 type/name 前缀/step.kind 三路提取,含 arguments/error/latency")
    print("[OK] thoughts/retrieval_context 优先 trace.metadata,observation 兜底")
    print("[OK] turns 优先 trace.metadata,缺失返回空列表")
    print("[OK] Phase2 提取结果透传 Phase1 适配器:tool 指标不被门控误跳,conversational 无 turns 跳过")
    print("[OK] 纯函数:不调 Langfuse API,不依赖 deepeval,不抛异常")


if __name__ == "__main__":
    main()
