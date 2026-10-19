"""onto-eval AI 对话式配置 — LLM 调用路由。

阶段 1：chat_completion 单轮调用 + parse_json_loose 容错解析。
阶段 2：chat_completion_with_tools 带 OpenAI function calling，返回原始 response 对象。
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable, Optional

import openai

from evaluators.llm_client import evaluate_with_llm, get_llm_client, _LLM_CALL_SEMAPHORE
from config import get_llm_config
from logger import log


async def chat_completion(
    system_prompt: str,
    user_prompt: str,
    model_key: Optional[str] = None,
) -> tuple[str, int]:
    """单轮 LLM 调用，返回 (文本, 估算tokens)。

    复用 evaluate_with_llm，自动走默认模型（model_key=None 时取 is_default 的 LLMModel）。
    8 并发信号量 + 429 重试由 evaluate_with_llm 内部处理。

    Args:
        system_prompt: 系统提示词
        user_prompt: 用户提示词
        model_key: 模型标识；None 则用默认模型

    Returns:
        (content_text, total_tokens)  total_tokens 为 0 时表示未拿到用量
    """
    content = await evaluate_with_llm(
        prompt=user_prompt,
        system_prompt=system_prompt,
        model_key=model_key,
    )
    # evaluate_with_llm 不返回 tokens 用量，这里用字符数粗估（仅用于落库记录，不用于计费）
    est_tokens = max(1, len(content) // 2) if content else 0
    log("INFO", "CHAT_LLM", "chat_completion 完成",
        model_key=model_key or "default",
        content_length=len(content) if content else 0)
    return content or "", est_tokens


def parse_json_loose(text: str) -> Optional[dict]:
    """容错 JSON 解析。

    LLM 要求只输出 JSON，但偶尔会带 markdown 围栏或前后缀文字。
    依次尝试：直接解析 → 提取 ```json 围栏 → 提取首个 {...} 块。
    """
    if not text:
        return None
    text = text.strip()
    # 1. 直接解析
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    # 2. 提取 ```json ... ``` 围栏
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(1))
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, ValueError):
            pass
    # 3. 提取首个 {...} 块（贪婪到最外层闭合）
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


# ═══════════════════════════════════════════════════════════════════════
# 阶段 2：tool-calling 支持
# ═══════════════════════════════════════════════════════════════════════

# 429 速率限制重试配置（复用 llm_client 的常量值）
_T_MAX_RETRIES = 5
_T_RETRY_BASE_DELAY = 10.0

# 连接重置/超时重试（envoy 网关断流、provider 抖动等瞬时故障）
_T_CONN_MAX_RETRIES = 4
_T_CONN_BASE_DELAY = 2.0


async def chat_completion_with_tools(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model_key: Optional[str] = None,
    on_chunk: Optional[Callable[[str], Awaitable[None]]] = None,
) -> dict:
    """带 tool-calling 的流式 LLM 调用，返回 dict。

    复用 get_llm_client + _LLM_CALL_SEMAPHORE(8并发) + 429 重试 + 连接重置重试。
    流式输出：文本 delta 通过 on_chunk 回调推送（同步调用，不阻塞）。
    tool_calls 在流中累积重组为完整列表。

    Args:
        messages: OpenAI 消息格式
        tools: OpenAI function schema 清单
        model_key: 模型标识；None 则用默认模型
        on_chunk: 文本增量回调（每个 delta 调一次）。已推送过 chunk 后不再重试。

    Returns:
        {"content": str, "tool_calls": list[dict], "finish_reason": str}
        tool_calls 元素格式:
            {"id": str, "type": "function",
             "function": {"name": str, "arguments": str}}

    Raises:
        RuntimeError: model_key 为 None 且无默认模型
        openai.RateLimitError: 429 重试耗尽
        Exception: 其他 LLM 调用错误
    """
    config = get_llm_config(model_key)
    client = get_llm_client(model_key)

    async with _LLM_CALL_SEMAPHORE:
        for _attempt in range(_T_MAX_RETRIES + 1):
            chunks_emitted = False
            try:
                stream = await client.chat.completions.create(
                    model=config["model"],
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=config.get("temperature", 0.0),
                    max_tokens=config.get("max_tokens", 4096),
                    timeout=config.get("timeout", 180.0),
                    stream=True,
                )

                content_parts: list[str] = []
                # tool_calls 按 index 累积：{index: {"id":, "name":, "arguments": str}}
                tc_accum: dict[int, dict[str, Any]] = {}
                finish_reason: Optional[str] = None

                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta

                    # 文本增量
                    text_delta = getattr(delta, "content", None)
                    if text_delta:
                        content_parts.append(text_delta)
                        chunks_emitted = True
                        if on_chunk:
                            try:
                                await on_chunk(text_delta)
                            except Exception as exc:
                                log("WARNING", "CHAT_LLM", "on_chunk 回调失败",
                                    error=str(exc))

                    # tool_calls 增量累积
                    tc_deltas = getattr(delta, "tool_calls", None)
                    if tc_deltas:
                        for tc in tc_deltas:
                            idx = tc.index if tc.index is not None else 0
                            slot = tc_accum.setdefault(idx, {
                                "id": "",
                                "name": "",
                                "arguments": "",
                            })
                            if tc.id:
                                slot["id"] = tc.id
                            fn = getattr(tc, "function", None)
                            if fn:
                                if fn.name:
                                    slot["name"] = fn.name
                                if fn.arguments:
                                    slot["arguments"] += fn.arguments

                    if getattr(choice, "finish_reason", None):
                        finish_reason = choice.finish_reason

                # 重组 tool_calls 列表（按 index 排序）
                tool_calls_out = []
                for idx in sorted(tc_accum.keys()):
                    slot = tc_accum[idx]
                    if slot["name"] or slot["arguments"]:
                        tool_calls_out.append({
                            "id": slot["id"] or f"call_{idx}",
                            "type": "function",
                            "function": {
                                "name": slot["name"],
                                "arguments": slot["arguments"] or "{}",
                            },
                        })

                content = "".join(content_parts)
                has_tools = bool(tool_calls_out)
                log("INFO", "CHAT_LLM", "chat_completion_with_tools 流式完成",
                    model_key=model_key or "default",
                    has_tool_calls=has_tools,
                    finish_reason=finish_reason,
                    content_length=len(content),
                    tool_calls_count=len(tool_calls_out),
                    attempt=_attempt + 1)

                return {
                    "content": content,
                    "tool_calls": tool_calls_out,
                    "finish_reason": finish_reason or ("tool_calls" if has_tools else "stop"),
                }
            except openai.RateLimitError as e:
                # 已推送过 chunk 则不再重试（避免前端文本重复）
                if chunks_emitted:
                    log("ERROR", "CHAT_LLM", "流式中途触发速率限制，不再重试",
                        model_key=model_key or "default", error=str(e))
                    raise
                if _attempt < _T_MAX_RETRIES:
                    wait = _T_RETRY_BASE_DELAY * (_attempt + 1)
                    log("WARNING", "CHAT_LLM", "触发速率限制，等待后重试",
                        model_key=model_key or "default",
                        attempt=_attempt + 1, max_retries=_T_MAX_RETRIES,
                        wait_seconds=wait, error=str(e))
                    await asyncio.sleep(wait)
                    continue
                raise
            except (openai.APIConnectionError, openai.APITimeoutError) as e:
                # envoy 网关断流 / 连接重置 / 上游处理超时等瞬时故障
                if chunks_emitted:
                    log("ERROR", "CHAT_LLM", "流式中途连接重置，不再重试",
                        model_key=model_key or "default",
                        error_type=type(e).__name__, error=str(e))
                    raise
                if _attempt < _T_CONN_MAX_RETRIES:
                    wait = _T_CONN_BASE_DELAY * (_attempt + 1)
                    log("WARNING", "CHAT_LLM", "LLM 连接重置/超时，等待后重试",
                        model_key=model_key or "default",
                        attempt=_attempt + 1, max_retries=_T_CONN_MAX_RETRIES,
                        wait_seconds=wait, error_type=type(e).__name__,
                        error=str(e))
                    await asyncio.sleep(wait)
                    continue
                raise
            except Exception as e:
                log("ERROR", "CHAT_LLM", "chat_completion_with_tools 失败",
                    model_key=model_key or "default",
                    error_type=type(e).__name__, error=str(e))
                raise
