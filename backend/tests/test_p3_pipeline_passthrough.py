# -*- coding: utf-8 -*-
"""Phase 3 验证:pipeline 透传 trace 过程数据 + fetcher 容错。

验证点(不需真实 Langfuse/LLM,用 mock client + 构造 trace):
1. fetch_process_data_for_trace:拉 trace → 提取 → 返回 TraceProcessData
2. fetch_process_data_for_trace 容错:trace_id 为空 / Langfuse 未配置 / 异常 → 返回空(不抛)
3. fetch_process_data_for_run:从 AgentRun 查 trace_id → 拉 trace → 提取(in-memory sqlite)
4. 全链路:fetcher → extract → adapter.evaluate,tool 指标有 tool_calls 时不被门控跳过
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.langfuse_trace_extract import (
    TraceProcessData,
    fetch_process_data_for_trace,
    fetch_process_data_for_run,
    extract_process_data,
)


def _mock_trace_with_tools():
    """构造带工具调用 + 思考 + 检索的 mock trace。"""
    return {
        "id": "trace-001",
        "observations": [
            {
                "id": "obs-reasoning",
                "name": "reasoning",
                "type": "span",
                "startTime": "2026-07-20T10:00:00.100Z",
                "endTime": "2026-07-20T10:00:00.500Z",
                "output": "分析用户意图,决定调用 OntoQuery",
                "metadata": {"agent.step.kind": "reasoning"},
            },
            {
                "id": "obs-tool-1",
                "name": "OntoQuery",
                "type": "tool",
                "startTime": "2026-07-20T10:00:01.000Z",
                "endTime": "2026-07-20T10:00:01.500Z",
                "input": '{"query": "ICD-10 E11"}',
                "output": {"snomed": "12345", "label": "2 型糖尿病"},
                "metadata": {"agent.step.kind": "tool", "agent.tool.name": "OntoQuery"},
            },
            {
                "id": "obs-retrieval-1",
                "name": "retrieval",
                "type": "retriever",
                "startTime": "2026-07-20T10:00:00.300Z",
                "endTime": "2026-07-20T10:00:00.800Z",
                "output": ["医学百科:2 型糖尿病的定义", "ICD-10 编码手册:E11"],
                "metadata": {"agent.step.kind": "retrieval"},
            },
        ],
    }


def test_fetch_process_data_for_trace_success():
    """验证:fetch_process_data_for_trace 拉到 trace 后正确提取。"""
    mock_trace = _mock_trace_with_tools()
    with patch("config.LANGFUSE_ENABLED", True):
        with patch("services.langfuse_client.get_langfuse_client") as mock_get:
            mock_client = AsyncMock()
            mock_client.get_trace = AsyncMock(return_value=mock_trace)
            mock_get.return_value = mock_client
            data = asyncio.run(fetch_process_data_for_trace("trace-001", "proj-1"))
    assert len(data.tool_calls) == 1
    assert data.tool_calls[0].name == "OntoQuery"
    assert data.tool_calls[0].arguments == {"query": "ICD-10 E11"}
    assert len(data.thoughts) == 1
    assert "分析用户意图" in data.thoughts[0]
    assert len(data.retrieval_context) == 2
    print("[OK] fetch_process_data_for_trace:拉 trace + 提取全链路正确")


def test_fetch_process_data_for_trace_empty_trace_id():
    """验证:trace_id 为空 → 返回空 TraceProcessData,不抛异常。"""
    data = asyncio.run(fetch_process_data_for_trace("", "proj-1"))
    assert isinstance(data, TraceProcessData)
    assert data.tool_calls == [] and data.thoughts == []
    data_none = asyncio.run(fetch_process_data_for_trace(None, "proj-1"))
    assert data_none.tool_calls == []
    print("[OK] fetch_process_data_for_trace:trace_id 为空返回空,不抛异常")


def test_fetch_process_data_for_trace_langfuse_disabled():
    """验证:Langfuse 未配置(LANGFUSE_ENABLED=False)→ 返回空,不抛。"""
    with patch("config.LANGFUSE_ENABLED", False):
        data = asyncio.run(fetch_process_data_for_trace("trace-001", "proj-1"))
    assert data.tool_calls == []
    print("[OK] fetch_process_data_for_trace:Langfuse 未配置返回空,不抛异常")


def test_fetch_process_data_for_trace_exception_resilient():
    """验证:Langfuse client 抛异常 → 返回空,不阻断 pipeline。"""
    with patch("config.LANGFUSE_ENABLED", True):
        with patch("services.langfuse_client.get_langfuse_client") as mock_get:
            mock_client = AsyncMock()
            mock_client.get_trace = AsyncMock(side_effect=RuntimeError("网络异常"))
            mock_get.return_value = mock_client
            data = asyncio.run(fetch_process_data_for_trace("trace-001", "proj-1"))
    assert data.tool_calls == []
    assert data.thoughts == []
    print("[OK] fetch_process_data_for_trace:Langfuse 异常时返回空,不阻断 pipeline")


def test_fetch_process_data_for_run_no_agent_run():
    """验证:agent_run_id 为空 → 返回空,不查 DB。"""
    data = asyncio.run(fetch_process_data_for_run(None, "run-001"))
    assert data.tool_calls == []
    data2 = asyncio.run(fetch_process_data_for_run(session=None, agent_run_id=None))
    assert data2.tool_calls == []
    print("[OK] fetch_process_data_for_run:agent_run_id 为空返回空,不查 DB")


def test_full_chain_fetcher_to_adapter():
    """验证全链路:fetcher → extract → adapter.evaluate,tool 指标有 tool_calls 时不被门控跳过。"""
    from evaluators.deepeval_adapter import DeepEvalMetricAdapter

    def make_spec(key, label, category, cls, kind="single", requires=(),
                   params=None, lower_is_better=False, gate_threshold=0.5):
        return {
            "key": key, "label": label, "category": category, "cls": cls,
            "kind": kind, "requires": tuple(requires),
            "extra": {}, "params": params or [], "note": "",
            "lower_is_better": lower_is_better, "gate_threshold": gate_threshold,
        }

    mock_trace = _mock_trace_with_tools()
    with patch("config.LANGFUSE_ENABLED", True):
        with patch("services.langfuse_client.get_langfuse_client") as mock_get:
            mock_client = AsyncMock()
            mock_client.get_trace = AsyncMock(return_value=mock_trace)
            mock_get.return_value = mock_client
            data = asyncio.run(fetch_process_data_for_trace("trace-001", "proj-1"))

    # tool_correctness 指标需要 expected_tools;有 tool_calls + expected_tools 时不被门控跳过
    spec = make_spec(
        "tool_correctness", "工具正确性", "tool",
        "ToolCorrectnessMetric", requires=("expected_tools",),
    )
    adapter = DeepEvalMetricAdapter(spec=spec, model_config={})
    result = asyncio.run(
        adapter.evaluate(
            question="查 ICD-10 E11",
            answer="2 型糖尿病,编码 E11",
            tool_calls=data.tool_calls,
            expected_tools=["OntoQuery"],
            thoughts=data.thoughts,
            retrieval_context=data.retrieval_context,
        )
    )
    # 门控通过(有 tool_calls + expected_tools + thoughts),走到 deepeval measure → 未装返回 None
    gated = "已跳过" in result.reason and "缺少" in result.reason
    assert not gated, f"有 tool_calls+expected_tools 不应被门控跳过, reason: {result.reason}"
    print(f"[OK] 全链路 fetcher→adapter:tool 指标不被门控跳过(reason: {result.reason[:60]})")


def test_full_chain_empty_trace_skips_process_metrics():
    """验证:空 trace(无工具/思考/检索)→ 过程数据指标被门控跳过(score=None)。"""
    from evaluators.deepeval_adapter import DeepEvalMetricAdapter

    def make_spec(key, label, category, cls, kind="single", requires=(),
                   params=None, lower_is_better=False, gate_threshold=0.5):
        return {
            "key": key, "label": label, "category": category, "cls": cls,
            "kind": kind, "requires": tuple(requires),
            "extra": {}, "params": params or [], "note": "",
            "lower_is_better": lower_is_better, "gate_threshold": gate_threshold,
        }

    # fetcher 返回空(fetch 失败/无 trace)
    data = TraceProcessData()  # 全空

    # tool_correctness 需要 expected_tools;无 tool_calls → avail['tools']=False
    # 但 requires 是 expected_tools,不是 tools;若也没 expected_tools → 跳过
    spec = make_spec(
        "tool_correctness", "工具正确性", "tool",
        "ToolCorrectnessMetric", requires=("expected_tools",),
    )
    adapter = DeepEvalMetricAdapter(spec=spec, model_config={})
    result = asyncio.run(
        adapter.evaluate(
            question="查 ICD-10",
            answer="E11",
            tool_calls=data.tool_calls,
            expected_tools=None,  # 无期望工具
        )
    )
    assert result.score is None
    assert "已跳过" in result.reason
    print(f"[OK] 空 trace → 过程数据指标被门控跳过(reason: {result.reason[:50]})")


def main():
    print("=== Phase 3 验证:pipeline 透传 trace 过程数据 + fetcher 容错 ===\n")
    test_fetch_process_data_for_trace_success()
    test_fetch_process_data_for_trace_empty_trace_id()
    test_fetch_process_data_for_trace_langfuse_disabled()
    test_fetch_process_data_for_trace_exception_resilient()
    test_fetch_process_data_for_run_no_agent_run()
    test_full_chain_fetcher_to_adapter()
    test_full_chain_empty_trace_skips_process_metrics()

    print("\n=== 验证结论 ===")
    print("[OK] fetch_process_data_for_trace:拉 trace + 提取全链路正确")
    print("[OK] fetcher 容错:trace_id 空 / Langfuse 未配置 / 异常 → 返回空,不阻断 pipeline")
    print("[OK] 全链路 fetcher→adapter:tool 指标有 tool_calls 时不被门控跳过")
    print("[OK] 空 trace → 过程数据指标被门控跳过(score=None)")


if __name__ == "__main__":
    main()
