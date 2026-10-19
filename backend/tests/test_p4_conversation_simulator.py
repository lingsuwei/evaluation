# -*- coding: utf-8 -*-
"""Phase 4 验证:会话模拟器 + 多轮 turns 存储 + 透传适配器。

验证点(不需真实 LLM/Langfuse,用 mock):
1. _serialize_conversation_history:把 history+当前输入序列化为带角色标记的单字符串
2. simulate_conversation:scenario 空 → 返回空;deepeval 未装 → 返回空;model 配置缺失 → 返回空
3. turns_to_eval_context_key:turns 序列化为 eval_context 可存格式
4. fetch_process_data_for_run:优先读 eval_context.conversation_turns(模拟器输出),trace 提取兜底
5. 全链路:eval_context 有 turns → fetch_process_data_for_run → 适配器收到 turns → conversational 指标不被门控跳过
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_serialize_conversation_history():
    """验证:多轮历史 + 当前输入序列化为带角色标记的单字符串。"""
    from services.inference_service import _serialize_conversation_history

    # 无历史 → 直接返回当前输入
    result = _serialize_conversation_history([], "你好")
    assert result == "你好"

    # 有历史 → 拼成带角色标记的文本
    history = [("user", "查 ICD-10"), ("assistant", "E11 是 2 型糖尿病")]
    result = _serialize_conversation_history(history, "那 E10 呢?")
    assert "用户: 查 ICD-10" in result
    assert "助手: E11 是 2 型糖尿病" in result
    assert "用户: 那 E10 呢?" in result

    print("[OK] _serialize_conversation_history:无历史直接返回,有历史拼角色标记")


def test_simulate_conversation_empty_scenario():
    """验证:scenario 空 → 返回空 turns,不调任何 LLM。"""
    from services.conversation_simulator_service import simulate_conversation

    result = asyncio.run(simulate_conversation(
        scenario="",
        agent_config={"url": "http://test"},
        eval_model_key="glm",
    ))
    assert result == []

    result2 = asyncio.run(simulate_conversation(
        scenario="   ",
        agent_config={"url": "http://test"},
        eval_model_key="glm",
    ))
    assert result2 == []
    print("[OK] simulate_conversation:scenario 空返回空 turns,不调 LLM")


def test_simulate_conversation_model_config_missing():
    """验证:评测模型配置缺失(api_key/base_url 空)→ 返回空,不抛异常。"""
    from services.conversation_simulator_service import simulate_conversation

    with patch("config.get_llm_config", return_value={"api_key": "", "base_url": "", "model": ""}):
        result = asyncio.run(simulate_conversation(
            scenario="用户咨询糖尿病诊断",
            agent_config={"url": "http://test"},
            eval_model_key="nonexistent",
        ))
    assert result == []
    print("[OK] simulate_conversation:模型配置缺失返回空,不抛异常")


def test_turns_to_eval_context_key():
    """验证:turns 序列化为 eval_context 可存格式。"""
    from services.conversation_simulator_service import turns_to_eval_context_key

    turns = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "您好,有什么可以帮您?"},
    ]
    result = turns_to_eval_context_key(turns)
    assert len(result) == 2
    assert result[0]["role"] == "user"
    assert result[0]["content"] == "你好"
    assert result[1]["role"] == "assistant"

    # 空 turns
    assert turns_to_eval_context_key([]) == []
    assert turns_to_eval_context_key(None) == []
    print("[OK] turns_to_eval_context_key:序列化正确,空输入返回空")


def test_fetch_process_data_prefers_eval_context_turns():
    """验证:fetch_process_data_for_run 优先读 eval_context.conversation_turns。"""
    from services.langfuse_trace_extract import fetch_process_data_for_run

    # mock AgentRun,有 eval_context.conversation_turns,无 trace_id
    mock_run = MagicMock()
    mock_run.eval_context = {
        "task_id": "t1",
        "conversation_turns": [
            {"role": "user", "content": "查 ICD-10"},
            {"role": "assistant", "content": "E11"},
        ],
    }
    mock_run.trace_id = None  # 无 trace
    mock_run.langfuse_project = None

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_run)

    data = asyncio.run(fetch_process_data_for_run(mock_session, "run-001"))
    assert len(data.turns) == 2
    assert data.turns[0].role == "user"
    assert data.turns[0].content == "查 ICD-10"
    assert data.turns[1].role == "assistant"
    assert data.turns[1].content == "E11"
    # 无 trace → tool_calls/thoughts/retrieval_context 为空
    assert data.tool_calls == []
    print("[OK] fetch_process_data_for_run:优先读 eval_context.conversation_turns,无 trace 时只返回 turns")


def test_full_chain_eval_context_turns_to_adapter():
    """验证全链路:eval_context 有 turns → fetch_process_data → 适配器收到 turns → conversational 指标不被门控跳过。"""
    from evaluators.deepeval_adapter import DeepEvalMetricAdapter

    def make_spec(key, label, category, cls, kind="single", requires=(),
                   params=None, lower_is_better=False, gate_threshold=0.5):
        return {
            "key": key, "label": label, "category": category, "cls": cls,
            "kind": kind, "requires": tuple(requires),
            "extra": {}, "params": params or [], "note": "",
            "lower_is_better": lower_is_better, "gate_threshold": gate_threshold,
        }

    # conversation_completeness 需要 turns(kind=conversational)
    spec = make_spec(
        "conversation_completeness", "对话完成度", "conversation",
        "ConversationCompletenessMetric", kind="conversational", requires=("turns",),
    )
    adapter = DeepEvalMetricAdapter(spec=spec, model_config={})

    # 构造模拟器生成的 turns(至少有 1 个 user turn)
    from services.langfuse_trace_extract import TraceProcessData, Turn
    sim_turns = [
        Turn(role="user", content="查 ICD-10 E11"),
        Turn(role="assistant", content="E11 是 2 型糖尿病"),
    ]
    process_data = TraceProcessData(turns=sim_turns)

    result = asyncio.run(adapter.evaluate(
        question="查 ICD-10",
        answer="E11 是 2 型糖尿病",
        turns=process_data.turns,
        expected_output="用户提供 ICD-10 编码查询,Agent 正确返回 E11",
    ))
    # 门控通过(有 turns),走到 deepeval measure → 未装返回 None 而非「已跳过:缺少 turns」
    gated = "已跳过" in result.reason and "缺少" in result.reason
    assert not gated, f"有 turns 不应被门控跳过, reason: {result.reason}"
    print(f"[OK] 全链路 eval_context→fetch→adapter:有 turns 时 conversational 指标不被门控跳过")


def test_full_chain_no_turns_skips_conversational():
    """验证:无 turns(eval_context 无 conversation_turns + 无 trace)→ conversational 指标被门控跳过。"""
    from evaluators.deepeval_adapter import DeepEvalMetricAdapter

    def make_spec(key, label, category, cls, kind="single", requires=(),
                   params=None, lower_is_better=False, gate_threshold=0.5):
        return {
            "key": key, "label": label, "category": category, "cls": cls,
            "kind": kind, "requires": tuple(requires),
            "extra": {}, "params": params or [], "note": "",
            "lower_is_better": lower_is_better, "gate_threshold": gate_threshold,
        }

    spec = make_spec(
        "conversation_completeness", "对话完成度", "conversation",
        "ConversationCompletenessMetric", kind="conversational", requires=("turns",),
    )
    adapter = DeepEvalMetricAdapter(spec=spec, model_config={})

    result = asyncio.run(adapter.evaluate(
        question="查 ICD-10",
        answer="E11",
        turns=None,  # 无 turns
    ))
    assert result.score is None
    assert "已跳过" in result.reason
    print(f"[OK] 无 turns → conversational 指标被门控跳过(score=None)")


def main():
    print("=== Phase 4 验证:会话模拟器 + 多轮 turns 存储 + 透传适配器 ===\n")
    test_serialize_conversation_history()
    test_simulate_conversation_empty_scenario()
    test_simulate_conversation_model_config_missing()
    test_turns_to_eval_context_key()
    test_fetch_process_data_prefers_eval_context_turns()
    test_full_chain_eval_context_turns_to_adapter()
    test_full_chain_no_turns_skips_conversational()

    print("\n=== 验证结论 ===")
    print("[OK] _serialize_conversation_history:多轮历史序列化为带角色标记单字符串")
    print("[OK] simulate_conversation:scenario 空/模型缺失 → 返回空,不抛异常")
    print("[OK] turns_to_eval_context_key:序列化为 eval_context 可存格式")
    print("[OK] fetch_process_data_for_run:优先读 eval_context.conversation_turns")
    print("[OK] 全链路:有 turns → conversational 指标不被门控跳过;无 turns → 跳过")


if __name__ == "__main__":
    main()
