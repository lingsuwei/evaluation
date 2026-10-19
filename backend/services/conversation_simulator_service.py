# -*- coding: utf-8 -*-
"""会话模拟器(Phase 4):用评测模型扮演"用户",与被测 Agent 多轮交互,生成 turns。

复用 DeepEval 的 ConversationSimulator(像 agent_eval-main-cmb 那样包一层适配):
- simulator_model = 评测模型(EvaluationTask.llm_model → get_llm_config → OpenAIModel)
- agent_callback = InferenceService.call_inference_multiturn(序列化 history+input → 单轮 HTTP)
- 输入 = DatasetItem 的 scenario/user_description/expected_outcome(自然语言,不写死 turns)
- 输出 = list[{role, content}](给 conversational 指标 + 存 AgentRun.eval_context)

设计要点:
1. deepeval 未装 → 返回空 turns(指标自动跳过)
2. 评测模型未配置(api_key/base_url 空) → 返回空
3. ConversationSimulator 是同步 API,用 asyncio.to_thread 在线程中运行;
   agent_callback 内部用 asyncio.run() 调 async inference(线程中无事件循环,不冲突)
4. 任何失败 → 返回空,不阻断 pipeline(指标自动跳过)
"""
import asyncio
from typing import Any, Dict, List, Optional

from services.inference_service import InferenceService


def _build_deepeval_model(eval_model_key: str):
    """从 onto-eval ModelConfig 构造 DeepEval 评测模型实例。

    用 get_llm_config 取 base_url/api_key/model,构造 OpenAIModel。
    DeepEval 新版用 base_url,旧版用 api_base(try/except 兼容)。
    """
    from config import get_llm_config

    cfg = get_llm_config(eval_model_key)
    if not cfg.get("api_key") or not cfg.get("base_url"):
        return None

    try:
        from deepeval.models import OpenAIModel
    except ImportError:
        return None

    model_name = cfg.get("model") or "gpt-4o"
    api_key = cfg.get("api_key", "")
    base_url = cfg.get("base_url", "")
    try:
        return OpenAIModel(model=model_name, api_key=api_key, base_url=base_url)
    except TypeError:
        # 旧版 DeepEval 用 api_base 参数
        try:
            return OpenAIModel(model=model_name, api_key=api_key, api_base=base_url)
        except Exception:
            return None
    except Exception:
        return None


async def simulate_conversation(
    scenario: str,
    agent_config: Dict[str, Any],
    eval_model_key: str,
    user_description: Optional[str] = None,
    expected_outcome: Optional[str] = None,
    max_turns: int = 4,
    agent_run_id: Optional[str] = None,
    langfuse_project: Optional[str] = None,
    logical_agent_id: Optional[str] = None,
    agent_version: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> List[Dict[str, str]]:
    """模拟多轮对话,返回 turns [{role: "user"|"assistant", content: str}, ...]。

    Args:
        scenario: 多轮对话场景描述(自然语言,给模拟器 LLM 看的)
        agent_config: 被测 Agent 推理配置(url/endpoint/headers/auth/payload_template)
        eval_model_key: 评测模型标识(如 "glm"),用于构造 simulator_model
        user_description: 用户画像(自然语言,让模拟器生成符合角色的 user 回合)
        expected_outcome: 期望结果(给 conversation_completeness 等 LLM judge 指标用)
        max_turns: 最大用户回合数(默认 4)
        agent_run_id: AgentRun.id(推理 trace 锚点用)
        langfuse_project/logical_agent_id/agent_version: 透传给 call_inference_multiturn

    Returns:
        turns 列表;失败返回空列表(指标自动跳过,不抛异常)
    """
    if not scenario or not scenario.strip():
        return []

    # 构造 simulator model(评测模型)
    simulator_model = _build_deepeval_model(eval_model_key)
    if simulator_model is None:
        return []

    # lazy import deepeval(门控之后)
    try:
        from deepeval.dataset import ConversationalGolden
        from deepeval.simulator import ConversationSimulator
        from deepeval.test_case import Turn
    except ImportError:
        return []

    # agent_callback:同步函数,ConversationSimulator 调它取 assistant 回复
    # 签名对齐 DeepEval:callback(input: str, turns: list[Turn]) -> Turn
    def agent_callback(input: str, turns) -> Turn:
        # turns 是完整历史(含最新 user 回合);history 是除最新回合外的历史
        history = [(t.role, str(t.content)) for t in (turns or [])[:-1]]
        # 线程中无事件循环,asyncio.run() 安全
        result = asyncio.run(
            InferenceService.call_inference_multiturn(
                user_input=input,
                history=history,
                config=agent_config,
                agent_run_id=agent_run_id,
                langfuse_project=langfuse_project,
                logical_agent_id=logical_agent_id,
                agent_version=agent_version,
                agent_id=agent_id,
            )
        )
        answer = (result.get("model_answer") or "").strip()
        return Turn(role="assistant", content=answer or "(Agent 无响应)")

    simulator = ConversationSimulator(
        model_callback=agent_callback,
        simulator_model=simulator_model,
        language="Chinese",
        async_mode=False,
        max_concurrent=1,
    )

    golden = ConversationalGolden(
        scenario=scenario,
        user_description=user_description or None,
        expected_outcome=expected_outcome or None,
    )

    try:
        # ConversationSimulator.simulate 是同步 API;
        # 用 asyncio.to_thread 在线程中运行,避免阻塞事件循环
        conversations = await asyncio.to_thread(
            simulator.simulate,
            conversational_goldens=[golden],
            max_user_simulations=max_turns,
        )
    except Exception:
        return []

    if not conversations:
        return []

    # 提取 turns(DeepEval 返回的 conversation 对象有 .turns 属性)
    convo = conversations[0]
    raw_turns = getattr(convo, "turns", None) or []
    turns: List[Dict[str, str]] = []
    for t in raw_turns:
        role = str(getattr(t, "role", "")).lower()
        content = str(getattr(t, "content", "") or "")
        if role in ("user", "assistant") and content:
            turns.append({"role": role, "content": content})
    return turns


def turns_to_eval_context_key(turns: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """把 turns 序列化为 eval_context.conversation_turns 可存的格式。

    当前 turns 已经是 [{role, content}, ...] dict 列表,直接返回。
    保留这个函数作为序列化点(未来格式变更只改这里)。
    """
    return [{"role": t.get("role", ""), "content": t.get("content", "")} for t in (turns or [])]
