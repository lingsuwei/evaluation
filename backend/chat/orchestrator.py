"""onto-eval AI 对话式配置 — Agent Loop（阶段 2）。

多轮 tool-calling 状态机：LLM 通过 OpenAI function calling 主动调用配置类工具，
逐步收集缺失字段并填充 EvaluateRequest 草稿，全程通过 SSE 流式推送中间态给前端。

核心：run_agent_loop(session_id, model_key, max_rounds=10)
  每轮：加载历史 → 调 LLM(含工具) → 有 tool_calls 则执行工具 → 回到加载历史
  无 tool_calls 则 assistant 文本消息 → 结束

SSE 基础设施照搬 tasks.py:task_streams 模式（进程内 Queue 字典）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

import openai
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session, ChatSession, ChatMessage
from chat.llm_router import chat_completion_with_tools
from chat.tools import get_tool_schemas, execute_tool, get_tool_safety, SAFETY_REQUIRES_CONFIRM
from chat.session import append_message, list_messages, get_session, update_draft, set_pending_confirm, clear_pending_confirm
from logger import log

# Agent loop 层 LLM 瞬时故障重试（针对 SDK 层重试耗尽后仍偶发的网关断流）
# 命中时本轮重置，给用户友好提示后继续，不终止整个 agent loop
_ORCH_LLM_MAX_RETRIES = 2
_ORCH_LLM_BASE_DELAY = 3.0

# ⚠️ WARNING: SSE 流存储（进程内内存字典）
# 此字典仅存在于当前进程内存中，不支持多 Worker / 多实例部署。
# 若使用 uvicorn --workers > 1 或 K8s 多副本，SSE 事件将无法跨进程/实例送达，
# 导致前端对话页面收不到工具调用过程流式更新。
# 生产环境如需多 Worker，请改用 Redis Pub/Sub 或数据库轮询替代方案。
chat_streams: Dict[str, asyncio.Queue] = {}

# 写操作工具的确认信令：session_id → Future
# agent loop 执行到 requires_confirm 工具时创建 Future 并 await，暂停等待；
# confirm/cancel 端点 set_result 后恢复 agent loop。
# ⚠️ 同 chat_streams，仅进程内有效，不支持多 Worker
_confirm_futures: Dict[str, asyncio.Future] = {}


async def get_chat_queue(session_id: str) -> asyncio.Queue:
    """Get or create a queue for chat session streaming."""
    if session_id not in chat_streams:
        chat_streams[session_id] = asyncio.Queue()
    return chat_streams[session_id]


async def emit_chat_event(session_id: str, event_type: str, data: dict):
    """Emit an SSE event to the chat session's queue."""
    queue = await get_chat_queue(session_id)
    await queue.put({"event": event_type, "data": data})


# ═══════════════════════════════════════════════════════════════════════
# System Prompt
# ═══════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """你是 onto-eval 本体评测平台的 AI 助手。用户会用自然语言描述需求，
你需要通过调用工具收集信息、组装评测配置、启动评测、查询结果、生成报告。

查询类工具（直接执行，无需确认）：
- list_datasets: 列出所有数据集
- list_agents: 列出可用 Agent
- list_llm_models: 列出已配置的评测 LLM 模型
- list_metrics: 列出可用评测指标
- list_eval_scripts: 列出评估脚本
- list_ontology_packages: 列出本体包（BM25/文档库评估的参考文档目录）
- get_dataset_preview: 预览数据集字段
- recommend_metrics: 按意图推荐指标组合
- build_eval_request: 组装评测配置草稿（可多次调用，增量更新）

任务查询与报告工具（直接执行，无需确认）：
- get_task_status: 查询任务状态/进度（pending/processing/completed/failed/paused/cancelled）
- get_eval_detail: 获取任务评测详情摘要（总分+四维结论+失败题目前5条），用于生成简报
- get_round_results: 按题+轮次返回 judge 明细（score+reason），用于深挖失败原因
- export_report: 触发生成 xlsx 报告，前端自动下载
- compare_tasks: 对比 2-6 个任务的总分+四维徽章+差值

启动类工具（自主执行，无需确认卡）：
- launch_eval: 基于已组装草稿直接启动评估任务

写操作工具（需用户确认后才执行）：
- create_llm_model / update_llm_model / delete_llm_model: 创建/更新/删除 LLM 模型配置
- create_agent / update_agent / delete_agent: 注册/更新/删除 Agent
- create_eval_script / delete_eval_script: 创建/删除评估脚本

确认流程规则：
- 调用写操作工具后，系统弹出确认卡片让用户确认，你不会立即看到结果
- 用户确认后，操作结果以工具结果形式返回给你
- 用户取消后，你收到 {cancelled: true} 结果
- 写操作成功后，告知用户操作已完成

重要规则：
- create_llm_model 不含 api_key 字段，创建后提醒用户到模型管理页填写 API Key
- create_agent 建议设 skip_probe=true，避免健康预检导致超时（测试环境必须）
- update_agent 不能修改 logical_agent_id 和 version（创建后固定）
- 删除操作不可逆，执行前向用户确认意图
- 不要编造资源 ID，操作前先用 list_* 查询确认存在

工作流程（配置评测）：
1. 用户描述意图后，先用 list_* 工具确认数据集/Agent/模型存在
2. 用 recommend_metrics 获取指标推荐
3. 用 build_eval_request 组装草稿（可多次调用，每次增量更新字段）
4. 草稿完整后，直接调 launch_eval 启动评测（无需用户二次确认）
5. 用户提到创建/修改/删除资源时，调用对应写操作工具

工作流程（查询与报告）：
1. 用户问"任务跑到哪了/完成了吗"→ 调 get_task_status
2. 用户问"给个简报/结果如何"→ 调 get_task_status 确认完成后调 get_eval_detail，Markdown 输出总分+四维结论
3. 用户问"哪题失败/为什么扣分"→ 调 get_round_results 取 judge 明细
4. 用户要"导出报告/下载 Excel"→ 调 export_report，前端自动下载
5. 用户要"对比任务 A 和 B"→ 调 compare_tasks，Markdown 表格输出对比矩阵
6. 启动评测后可用 get_task_status 轮询状态，但不要死循环（最多 3 次）

评测规则：
- 在线评估(online_mode=true)必须有 agent_id
- 离线评估不需要 agent_id，enable_stability 建议设 false
- 用户没明确在线/离线，默认在线
- 轮次 inference_rounds 限制 1-10
- 离线评估时，用户提到"从Langfuse拉答案/用历史trace"等，用 build_eval_request 的 langfuse_source 字段填充
- 回复用中文，简洁"""


# 单会话累计 token 上限（防失控；正常配置对话 <5000 tokens）
# 模块 E：从 50000 提升到 80000，配合流式输出后单轮 token 可能更多
_MAX_SESSION_TOKENS = 80000
# tool 结果截断（仅传给 LLM 的 content，DB 保留完整）
# 模块 E：从 2000 提升到 4000，让 LLM 看到更完整的 eval 摘要
_TOOL_RESULT_MAX_CHARS = 4000
# 滑窗阈值：历史消息累计字符超过此值开始裁剪（粗估：4 字符 ≈ 1 token）
# 上限 60k 字符 ≈ 15k token；裁剪后保留最近 40k 字符 ≈ 10k token
_SLIDING_WINDOW_HIGH = 60000
_SLIDING_WINDOW_LOW = 40000


def _estimate_messages_chars(messages: list[dict[str, Any]]) -> int:
    """粗估 messages 总字符数（不含 system prompt）。"""
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif c:
            total += len(json.dumps(c, ensure_ascii=False))
        tcs = m.get("tool_calls")
        if tcs:
            total += len(json.dumps(tcs, ensure_ascii=False))
    return total


def _apply_sliding_window(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """滑窗裁剪：保留 system prompt + 最近消息使总字符落在 LOW-HIGH 区间。

    裁剪规则：
    - system prompt 永远保留（messages[0]）
    - 裁剪点必须落在"完整的 tool_call 组"之后（assistant.tool_calls 后面必须紧跟
      对应的 tool 消息，否则 OpenAI API 会报错），所以遇到 assistant 带 tool_calls
      时，要连同后续的 tool 消息一起保留或一起裁剪
    - 被裁剪的消息不删库，仅不放入本次 LLM 上下文
    """
    if len(messages) <= 1:
        return messages
    system_msg = messages[0]
    history = messages[1:]
    total = _estimate_messages_chars(history)
    if total <= _SLIDING_WINDOW_HIGH:
        return messages

    # 从最早的消息开始裁剪，直到总字符降到 LOW 以下
    # 但要保证裁剪点不在 tool_calls 组的中间
    cut_idx = 0
    accumulated = 0
    # 从后往前累加，直到超过 LOW
    target = total - _SLIDING_WINDOW_LOW
    running = 0
    for i, m in enumerate(history):
        running += _estimate_messages_chars([m])
        if running >= target:
            cut_idx = i + 1
            break
    else:
        cut_idx = len(history)

    # 调整 cut_idx：不能落在 assistant.tool_calls 之后但 tool 消息之前
    # 即如果 history[cut_idx-1] 是 assistant 带 tool_calls，
    # 且 history[cut_idx] 是 tool 消息，则必须把 cut_idx 后移到 tool 组之后
    while cut_idx < len(history):
        prev = history[cut_idx - 1] if cut_idx > 0 else None
        curr = history[cut_idx]
        if (prev and prev.get("role") == "assistant" and prev.get("tool_calls")
                and curr.get("role") == "tool"):
            cut_idx += 1
        else:
            break

    trimmed = history[cut_idx:]
    log("INFO", "CHAT_ORCH", "应用滑窗裁剪",
        original_count=len(history), kept_count=len(trimmed),
        original_chars=total, kept_chars=_estimate_messages_chars(trimmed))
    return [system_msg] + trimmed


def _serialize_messages(
    history: list[ChatMessage],
    system_prompt: str,
) -> list[dict[str, Any]]:
    """把 ChatMessage 历史 + system prompt 序列化为 OpenAI messages 格式。

    - user: {role, content}
    - assistant(纯文本): {role, content}
    - assistant(带 tool_calls): {role, content, tool_calls}
    - tool: {role: "tool", tool_call_id, content}

    tool 结果按 _TOOL_RESULT_MAX_CHARS 截断（仅 LLM 上下文用，DB 保留完整结果审计），
    避免多轮 tool-calling 后 prompt 累计膨胀触发 LLM 网关连接重置。
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    for m in history:
        if m.role == "user":
            messages.append({"role": "user", "content": m.content or ""})
        elif m.role == "assistant":
            # tool_name=="tool_calls" 标识阶段2 assistant 带 tool_calls 的消息
            # OpenAI/GLM API 要求：assistant 发起 tool_calls 后，下一轮回传必须含
            # tool_calls 字段（含每个 tc.id），且后续 tool 消息的 tool_call_id 与之
            # 严格匹配。否则多 tool_call 时上游 LLM 处理崩溃 → 网关连接重置。
            if m.tool_name == "tool_calls" and m.tool_args:
                tool_calls_list: list[dict] = []
                try:
                    args_dict = json.loads(m.tool_args)
                    if isinstance(args_dict, dict) and args_dict.get("tool_calls"):
                        tool_calls_list = args_dict["tool_calls"]
                except (ValueError, TypeError):
                    pass
                if tool_calls_list:
                    messages.append({
                        "role": "assistant",
                        "content": m.content or "",
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": tc["arguments"],
                                },
                            }
                            for tc in tool_calls_list
                        ],
                    })
                else:
                    messages.append({"role": "assistant", "content": m.content or ""})
            else:
                messages.append({"role": "assistant", "content": m.content or ""})
        elif m.role == "tool":
            # tool 消息：tool_call_id 优先从 tool_args._tool_call_id 读（LLM 返回的
            # 真实 tc.id），向后兼容旧数据 fallback 到 f"call_{m.id}"
            tool_call_id = f"call_{m.id}"
            if m.tool_args:
                try:
                    args_dict = json.loads(m.tool_args)
                    if isinstance(args_dict, dict) and args_dict.get("_tool_call_id"):
                        tool_call_id = args_dict["_tool_call_id"]
                except (ValueError, TypeError):
                    pass

            tool_result_str = m.tool_result or "{}"
            if len(tool_result_str) > _TOOL_RESULT_MAX_CHARS:
                tool_result_str = (
                    tool_result_str[:_TOOL_RESULT_MAX_CHARS]
                    + f"\n...(结果已截断 {len(tool_result_str) - _TOOL_RESULT_MAX_CHARS} 字符，完整数据见数据库)"
                )
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": tool_result_str,
            })

    # 滑窗裁剪：超过 _SLIDING_WINDOW_HIGH 时保留最近消息
    messages = _apply_sliding_window(messages)
    return messages


def _build_tool_call_messages(
    assistant_msg: Any,
    tool_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """构造 assistant tool_calls 消息 + 对应 tool 结果消息。

    已废弃：orchestrator 内联处理 tool_calls，本函数保留供未来需要。
    注意：assistant_msg 现为 dict 格式（{content, tool_calls, finish_reason}），
         如需启用本函数请同步适配 dict 访问方式。
    """
    raise NotImplementedError("已废弃，请使用 orchestrator 内联逻辑")


async def run_agent_loop(
    session_id: str,
    model_key: Optional[str] = None,
    max_rounds: int = 10,
) -> None:
    """运行 agent loop，通过 SSE 推送中间态，结束后草稿落库。

    每轮：
    1. 加载历史 ChatMessage + system prompt(含工具清单)
    2. 调 llm_router.chat_completion_with_tools(messages, tools)
    3. 若有 tool_calls：执行工具 → append tool 消息 → emit chat_tool_call/result → 回到 1
    4. 若无 tool_calls：append assistant 消息 → emit chat_message → 结束
    """
    log("INFO", "CHAT_ORCH", "agent loop 启动",
        session_id=session_id, model_key=model_key or "default", max_rounds=max_rounds)

    await emit_chat_event(session_id, "chat_start", {
        "session_id": session_id,
        "model_key": model_key,
        "max_rounds": max_rounds,
    })

    tools = get_tool_schemas()
    total_tokens = 0

    # Langfuse trace：整个 agent loop 作为一个 span，session_id 作为 Langfuse session
    from services.langfuse_tracing import get_langfuse_client
    langfuse_client = get_langfuse_client()
    _trace_ctx = None
    if langfuse_client is not None:
        try:
            _trace_ctx = langfuse_client.start_as_current_observation(
                as_type="span",
                name=f"chat_session_{session_id}",
                input={"session_id": session_id, "model_key": model_key or "default"},
            )
            _trace_ctx.update(
                session_id=session_id,
                metadata={"source": "chat", "platform": "onto-eval"},
                tags=["chat"],
            )
        except Exception as exc:
            log("WARNING", "CHAT_ORCH", "langfuse trace 初始化失败",
                session_id=session_id, error=str(exc))
            _trace_ctx = None

    try:
        async with async_session() as db_session:
            # 标记会话进行中
            chat_session = await get_session(session_id, db_session)
            if not chat_session:
                await emit_chat_event(session_id, "chat_error", {
                    "error": f"会话 {session_id} 不存在", "round": 0,
                })
                return

            for round_num in range(1, max_rounds + 1):
                # 1. 加载历史消息
                history = await list_messages(session_id, db_session)
                messages = _serialize_messages(history, _SYSTEM_PROMPT)

                # 记录请求 payload 体量，便于诊断网关连接重置
                msg_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
                log("INFO", "CHAT_ORCH", "LLM 请求 payload 体量",
                    session_id=session_id, round=round_num,
                    message_count=len(messages), total_chars=msg_chars,
                    tool_schemas=len(tools))

                # 2. 调 LLM（瞬时故障重试，避免整轮 agent loop 终止）
                # 流式：每个 text delta 通过 on_chunk 回调实时 emit chat_message_chunk
                async def _on_chunk(text_delta: str):
                    await emit_chat_event(session_id, "chat_message_chunk", {
                        "content": text_delta,
                        "round": round_num,
                    })

                # Langfuse generation span（每轮 LLM 调用一个 generation）
                _gen_span = None
                if langfuse_client is not None:
                    try:
                        _gen_span = langfuse_client.start_as_current_observation(
                            as_type="generation",
                            name=f"chat_round_{round_num}",
                            model=model_key or "default",
                            input={"messages_count": len(messages), "round": round_num},
                        )
                    except Exception as exc:
                        log("WARNING", "CHAT_ORCH", "langfuse generation span 创建失败",
                            session_id=session_id, error=str(exc))
                        _gen_span = None

                assistant_msg = None
                for _llm_attempt in range(_ORCH_LLM_MAX_RETRIES + 1):
                    try:
                        assistant_msg = await chat_completion_with_tools(
                            messages, tools, model_key, on_chunk=_on_chunk,
                        )
                        break
                    except (openai.APIConnectionError, openai.APITimeoutError) as exc:
                        if _llm_attempt < _ORCH_LLM_MAX_RETRIES:
                            wait = _ORCH_LLM_BASE_DELAY * (_llm_attempt + 1)
                            log("WARNING", "CHAT_ORCH", "LLM 瞬时故障，本轮重试",
                                session_id=session_id, round=round_num,
                                attempt=_llm_attempt + 1, max_retries=_ORCH_LLM_MAX_RETRIES,
                                wait_seconds=wait, error_type=type(exc).__name__,
                                error=str(exc))
                            await emit_chat_event(session_id, "chat_message", {
                                "content": f"⚠️ LLM 网关连接重置，{wait:.0f}s 后重试（{_llm_attempt + 1}/{_ORCH_LLM_MAX_RETRIES}）…",
                                "round": round_num,
                            })
                            await asyncio.sleep(wait)
                            continue
                        # 重试耗尽：抛出，由外层 except 转 chat_error
                        raise
                if assistant_msg is None:
                    # 理论不可达（循环里要么 break 要么 raise），防御性兜底
                    raise RuntimeError("LLM 调用未返回响应")

                # 关闭 generation span
                if _gen_span is not None:
                    try:
                        _gen_span.update(
                            output={
                                "content": (assistant_msg.get("content") or "")[:1000],
                                "tool_calls_count": len(assistant_msg.get("tool_calls") or []),
                                "finish_reason": assistant_msg.get("finish_reason"),
                            },
                            metadata={"round": round_num, "tokens_estimated": total_tokens},
                        )
                        _gen_span.end()
                    except Exception as exc:
                        log("WARNING", "CHAT_ORCH", "langfuse generation span 结束失败",
                            session_id=session_id, error=str(exc))

                # 流式模式不返回 usage，按内容字符数粗估
                content_chars = len(assistant_msg.get("content") or "")
                if content_chars:
                    total_tokens += max(1, content_chars // 2)
                    if total_tokens > _MAX_SESSION_TOKENS:
                        log("WARNING", "CHAT_ORCH", "会话 token 超限，终止",
                            session_id=session_id, total_tokens=total_tokens,
                            limit=_MAX_SESSION_TOKENS)
                        await emit_chat_event(session_id, "chat_error", {
                            "error": f"会话累计 token 超限({total_tokens} > {_MAX_SESSION_TOKENS})",
                            "round": round_num,
                        })
                        return

                tool_calls = assistant_msg.get("tool_calls") or []

                # 3. 有 tool_calls → 执行工具
                if tool_calls:
                    # 先把 assistant tool_calls 消息入库（OpenAI API 要求 assistant
                    # tool_calls 在 tool 结果消息之前；之前顺序反了，多 tool_call
                    # 时触发上游 LLM 处理崩溃 → 网关连接重置）
                    await append_message(
                        session_id, role="assistant",
                        content=assistant_msg.get("content"),
                        tool_name="tool_calls",
                        tool_args={
                            "tool_calls": [
                                {"id": tc["id"], "name": tc["function"]["name"],
                                 "arguments": tc["function"]["arguments"]}
                                for tc in tool_calls
                            ]
                        },
                        session=db_session,
                    )

                    tool_results: list[dict[str, Any]] = []
                    for tc in tool_calls:
                        tool_name = tc["function"]["name"]
                        try:
                            tool_args = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
                        except (json.JSONDecodeError, ValueError):
                            tool_args = {}

                        await emit_chat_event(session_id, "chat_tool_call", {
                            "tool_name": tool_name,
                            "tool_args": tool_args,
                            "round": round_num,
                        })

                        # 写操作工具：暂停等待用户确认
                        safety = get_tool_safety(tool_name)
                        if safety == SAFETY_REQUIRES_CONFIRM:
                            # 存待确认操作
                            await set_pending_confirm(
                                session_id,
                                {"tool_name": tool_name, "tool_args": tool_args},
                                db_session,
                            )
                            # emit 确认事件，前端弹确认卡片
                            await emit_chat_event(session_id, "chat_confirm_required", {
                                "tool_name": tool_name,
                                "tool_args": tool_args,
                                "round": round_num,
                            })
                            # 创建 Future 并等待，agent loop 暂停
                            future: asyncio.Future = asyncio.get_event_loop().create_future()
                            _confirm_futures[session_id] = future
                            try:
                                confirm_result = await future
                            finally:
                                _confirm_futures.pop(session_id, None)

                            if confirm_result.get("cancelled"):
                                result = {"cancelled": True, "message": "用户取消了操作"}
                            else:
                                result = confirm_result.get("result", {})
                                await clear_pending_confirm(session_id, db_session)
                        else:
                            # 查询/草稿/启动类工具：直接执行
                            result = await execute_tool(
                                tool_name, db_session, session_id, tool_args,
                            )

                        await emit_chat_event(session_id, "chat_tool_result", {
                            "tool_name": tool_name,
                            "result_summary": _summarize_result(result),
                            "result": result,
                            "round": round_num,
                        })

                        # 追加 tool 消息到 DB；tool_args 加 _tool_call_id 供
                        # _serialize_messages 恢复与 assistant.tool_calls 匹配的 id
                        await append_message(
                            session_id, role="tool",
                            content=json.dumps(result, ensure_ascii=False),
                            tool_name=tool_name,
                            tool_args={**tool_args, "_tool_call_id": tc["id"]},
                            tool_result=result,
                            session=db_session,
                        )

                        tool_results.append({
                            "tool_call_id": tc["id"],
                            "tool_name": tool_name,
                            "result": result,
                        })

                    log("INFO", "CHAT_ORCH", f"第 {round_num} 轮工具调用完成",
                        session_id=session_id, round=round_num,
                        tools=[tr["tool_name"] for tr in tool_results])
                    continue  # 回到 LOOP 顶部

                # 4. 无 tool_calls → assistant 文本消息，结束
                content = assistant_msg.get("content") or ""
                await append_message(
                    session_id, role="assistant",
                    content=content,
                    session=db_session,
                )
                # stream 中已逐 chunk emit chat_message_chunk；
                # 这里 emit 一次 chat_message 作为"定稿"信号，前端据此停止增量更新
                await emit_chat_event(session_id, "chat_message", {
                    "content": content,
                    "round": round_num,
                })

                # 标记会话 ready（草稿就绪）
                chat_session = await get_session(session_id, db_session)
                if chat_session and chat_session.status == "draft":
                    chat_session.status = "ready"
                    await db_session.commit()

                await emit_chat_event(session_id, "chat_complete", {
                    "session_id": session_id,
                    "draft_ready": True,
                })
                log("INFO", "CHAT_ORCH", "agent loop 正常结束",
                    session_id=session_id, rounds=round_num, total_tokens=total_tokens)
                return

            # 超过 max_rounds
            log("WARNING", "CHAT_ORCH", "agent loop 超轮终止",
                session_id=session_id, max_rounds=max_rounds)
            await emit_chat_event(session_id, "chat_error", {
                "error": f"超过最大轮数 {max_rounds}，对话终止",
                "round": max_rounds,
            })

    except Exception as exc:
        log("ERROR", "CHAT_ORCH", "agent loop 异常",
            session_id=session_id, error=str(exc), error_type=type(exc).__name__)
        await emit_chat_event(session_id, "chat_error", {
            "error": f"agent loop 异常: {exc}",
            "round": 0,
        })
    finally:
        if _trace_ctx is not None:
            try:
                _trace_ctx.update(output={"status": "completed"})
                _trace_ctx.end()
            except Exception as exc:
                log("WARNING", "CHAT_ORCH", "langfuse trace 结束失败",
                    session_id=session_id, error=str(exc))
        if langfuse_client is not None:
            try:
                langfuse_client.flush()
            except Exception:
                pass


def _summarize_result(result: dict, max_len: int = 300) -> str:
    """把工具结果摘要为给前端预览的字符串。"""
    try:
        text = json.dumps(result, ensure_ascii=False)
        if len(text) <= max_len:
            return text
        return text[:max_len] + "…"
    except (TypeError, ValueError):
        return str(result)[:max_len]
