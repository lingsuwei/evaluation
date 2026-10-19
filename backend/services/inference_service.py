"""
通用推理服务
提供调用外部推理接口的能力
"""
import asyncio
import time
import json
import base64
import aiohttp
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, TYPE_CHECKING
from logger import log
from utils.agent_url import resolve_agent_base_url

if TYPE_CHECKING:
    from services.langfuse_tracing import TraceContext


def _is_token_expired(token: str) -> bool:
    """
    检查 JWT Token 是否过期

    Args:
        token: JWT Token 字符串

    Returns:
        True 如果 token 已过期或无效，False 如果未过期
    """
    if not token:
        return True

    try:
        # JWT 格式: header.payload.signature
        parts = token.split('.')
        if len(parts) != 3:
            return True

        # 解码 payload
        payload_b64 = parts[1]
        # 补充 padding
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += '=' * padding

        payload_json = base64.b64decode(payload_b64).decode('utf-8')
        payload = json.loads(payload_json)

        # 检查 exp 字段
        exp = payload.get('exp')
        if not exp:
            return True

        # 获取当前时间戳
        import time
        current_time = int(time.time())

        # 如果过期时间小于当前时间，说明已过期
        # 预留 60 秒的缓冲时间
        return exp - current_time < 60

    except Exception as e:
        log("WARNING", "INFERENCE", "Token 过期检查失败", error=str(e))
        return True


async def _refresh_token(base_url: str, username: str, password: str) -> Optional[str]:
    """
    使用用户名和密码重新登录获取新 Token

    Args:
        base_url: Agent 基础 URL
        username: 用户名
        password: 密码

    Returns:
        新的 token 或 None（如果登录失败）
    """
    if not username or not password:
        log("ERROR", "INFERENCE", "无法刷新 Token：用户名或密码为空")
        return None

    try:
        login_url = f"{base_url.rstrip('/')}/auth/login"

        log("INFO", "INFERENCE", "尝试自动刷新 Token", url=login_url, username=username)

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                login_url,
                json={"username": username, "password": password}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    new_token = data.get("token")
                    if new_token:
                        log("INFO", "INFERENCE", "Token 刷新成功")
                        return new_token
                    else:
                        log("ERROR", "INFERENCE", "Token 刷新失败：响应中没有 token")
                        return None
                else:
                    error_text = await response.text()
                    log("ERROR", "INFERENCE", "Token 刷新失败",
                        status_code=response.status, error=error_text)
                    return None

    except Exception as e:
        log("ERROR", "INFERENCE", "Token 刷新异常", error=str(e))
        return None


async def _persist_refreshed_token(agent_id: str, new_token: str) -> bool:
    """将刷新后的 token 回写到数据库 agent.inference_config。

    避免每次推理都重复登录。失败仅告警，不阻塞推理流程。
    """
    if not agent_id or not new_token:
        return False
    try:
        from database import async_session, Agent
        from sqlalchemy import select
        import json as _json

        async with async_session() as session:
            result = await session.execute(
                select(Agent).where(Agent.id == agent_id)
            )
            agent = result.scalar_one_or_none()
            if not agent or not agent.inference_config:
                return False
            cfg = _json.loads(agent.inference_config)
            auth = cfg.get("auth") or {}
            auth["token"] = new_token
            cfg["auth"] = auth
            agent.inference_config = _json.dumps(cfg)
            await session.commit()
        log("INFO", "INFERENCE", "刷新后的 Token 已回写数据库", agent_id=agent_id)
        return True
    except Exception as e:
        log("WARNING", "INFERENCE", "Token 回写数据库失败", agent_id=agent_id, error=str(e))
        return False


def _replace_in_dict(obj: dict, template_vars: dict) -> dict:
    """递归替换字典中的模板变量"""
    result = {}
    for key, value in obj.items():
        if isinstance(value, str):
            replaced = value
            for var_name, var_value in template_vars.items():
                replaced = replaced.replace(var_name, var_value)
            result[key] = replaced
        elif isinstance(value, list):
            result[key] = _replace_in_list(value, template_vars)
        elif isinstance(value, dict):
            result[key] = _replace_in_dict(value, template_vars)
        else:
            result[key] = value
    return result


def _replace_in_list(obj: list, template_vars: dict) -> list:
    """递归替换列表中的模板变量"""
    result = []
    for item in obj:
        if isinstance(item, str):
            replaced = item
            for var_name, var_value in template_vars.items():
                replaced = replaced.replace(var_name, var_value)
            result.append(replaced)
        elif isinstance(item, list):
            result.append(_replace_in_list(item, template_vars))
        elif isinstance(item, dict):
            result.append(_replace_in_dict(item, template_vars))
        else:
            result.append(item)
    return result


_PLACEHOLDER_VALUES = frozenset({
    "", "${question}", "{question}", "${input}", "{input}",
})


def _is_empty_or_placeholder(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return stripped in _PLACEHOLDER_VALUES


def _normalize_payload_template(raw: Any) -> dict:
    """兼容 payload_template 存成 JSON 字符串或空值的情况"""
    if raw is None:
        return {}
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            log("WARNING", "INFERENCE", "payload_template 不是合法 JSON 对象，将使用默认请求体")
            return {}
    return raw if isinstance(raw, dict) else {}


def _ensure_agentic_request(payload: dict, question: str, endpoint: str) -> None:
    """onto-agent /agentic/runs/sync 要求 request 非空"""
    if endpoint.lstrip("/") not in ("agentic/runs/sync", "agentic/runs"):
        return
    if _is_empty_or_placeholder(payload.get("request")):
        payload["request"] = question
        log(
            "INFO",
            "INFERENCE",
            "已修正 agentic/runs/sync 的 request 字段",
            question_preview=(question or "")[:50],
        )


_EVAL_SYNC_CONTEXT_ENDPOINTS = frozenset({
    # onto-agent 同步推理端点
    "agentic/runs/sync",
    # onto-agent 异步推理端点
    "agentic/runs",
    # agent_swarm 同步推理端点（对接 onto-eval）
    "api/chat/sync",
})


def _inject_eval_sync_context(
    payload: dict,
    *,
    endpoint: str,
    agent_run_id: Optional[str] = None,
    langfuse_project: Optional[str] = None,
    logical_agent_id: Optional[str] = None,
    agent_version: Optional[str] = None,
    task_id: Optional[str] = None,
) -> None:
    """为同步推理端点注入 eval 关联 context（H-01 Push）。

    支持的端点：
    - onto-agent /agentic/runs/sync
    - agent_swarm /api/chat/sync
    """
    if endpoint.lstrip("/") not in _EVAL_SYNC_CONTEXT_ENDPOINTS:
        return
    if not any([agent_run_id, langfuse_project, logical_agent_id, agent_version, task_id]):
        return
    ctx = payload.get("context")
    if not isinstance(ctx, dict):
        ctx = {}
        payload["context"] = ctx
    if agent_run_id:
        ctx["agent_run_id"] = agent_run_id
    if langfuse_project:
        ctx["langfuse_project"] = langfuse_project
    if logical_agent_id:
        ctx["logical_agent_id"] = logical_agent_id
    if agent_version:
        ctx["agent_version"] = agent_version
    if task_id:
        ctx["task_id"] = task_id
    ctx["supports_user_interaction"] = False


def _render_langfuse_output_as_text(output: Any) -> str:
    """把 Langfuse trace.output 渲染成可读文本，用于 final_answer 兜底。

    适配 onto-agent 的 output 结构：嵌套 dict + render_datas/table_info
    （带 remark.top_note / summary）→ markdown 风格文本；其它情况
    退化到最终字符串序列化。
    """
    if isinstance(output, str):
        return output.strip()
    if not isinstance(output, dict):
        return ""

    # 优先级 1：直接取 final_answer / answer / output / content / summary
    for key in ("final_answer", "answer", "output", "content", "summary", "result"):
        val = output.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            inner = _render_langfuse_output_as_text(val)
            if inner:
                return inner

    # 优先级 2：构造 markdown（render_datas / table_info）
    inner = output.get("output") if isinstance(output.get("output"), dict) else output
    parts: List[str] = []
    for rd in inner.get("render_datas") or []:
        for item in rd.get("data") or []:
            td = item.get("tableData") or {}
            title = td.get("table_title")
            if title:
                parts.append(f"# {title}")
            rmk = item.get("remark") or {}
            for rk in ("top_note", "summary", "bottom_note", "overview"):
                txt = (rmk.get(rk) or "").strip()
                if txt:
                    parts.append(txt)
            rows = td.get("rows") or []
            headers = td.get("headers") or []
            if rows and headers:
                col_names = [str(h.get("column_name", "")) for h in headers]
                parts.append("| " + " | ".join(col_names) + " |")
                parts.append("|" + "|".join(["---"] * len(col_names)) + "|")
                for row in rows:
                    vals = []
                    for v in row.get("values") or []:
                        s = str(v).replace("|", "\\|").replace("\n", " ").strip()
                        if len(s) > 240:
                            s = s[:240] + "…"
                        vals.append(s)
                    parts.append("| " + " | ".join(vals) + " |")

    text = "\n\n".join(p for p in parts if p).strip()
    if text:
        return text

    # 优先级 3：递归提取所有较长的 string 字段拼接
    collected: List[str] = []

    def _walk(obj: Any, depth: int = 0) -> None:
        if depth > 5 or len(collected) >= 12:
            return
        if isinstance(obj, str):
            s = obj.strip()
            if len(s) >= 6 and not s.startswith("{") and not s.startswith("["):
                collected.append(s[:400])
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v, depth + 1)

    _walk(output)
    return "\n".join(collected) if collected else ""


async def _fetch_answer_from_langfuse_with_retry(
    *,
    trace_id: str,
    langfuse_project: Optional[str],
    anchor_ts: Optional[Any] = None,
    max_attempts: int = 5,
) -> Optional[str]:
    """Langfuse 兜底：拉 trace 详情，提取可读文本答案。

    策略（按时间线）：
      T+0 响应回来：
        1) 按 onto-agent 返回的 trace_id 查（最快路径）。
        2) 查不到（404 / 资源不存在）→ 用 anchor_ts 时间窗 fallback：
           在 Langfuse 项目内按时间窗找最接近的一条业务 trace（剔除 onto-eval
           自报的评估包装 trace）。这覆盖 "onto-agent 同步接口给的 trace_id
           与实际写入 Langfuse 的 trace_id 不一致" 的已知场景。
      T+5min  第 1 次拉取（响应结束后 sleep 5min 让 Langfuse 落库再查）。
        拉到但 output 为空（等待异步落库）：
          首次空 → 30s 后再试
          仍空     → 5min 后再试

    最坏总等待（max_attempts=5）：
      全程 404：5min + 4×30s = 7min
      首次空 + 后续仍空：5min + 30s + 3×5min ≈ 20.5min
    """
    if not trace_id and not anchor_ts:
        return None
    try:
        from config import LANGFUSE_ENABLED
        from services.langfuse_client import get_langfuse_client

        if not LANGFUSE_ENABLED:
            return None
        client = get_langfuse_client(langfuse_project)
    except Exception as e:  # pragma: no cover - 防御性
        log("WARN", "INFERENCE", "构造 Langfuse 客户端失败，跳过兜底", error=str(e))
        return None

    FIRST_DELAY_SEC = 5 * 60  # 响应结束后等 5min 让 Langfuse 落库（output 异步填）
    POLL_INTERVAL_SEC = 30     # 5min 后仍未拿到时 30s 轮询

    actual_trace_id = trace_id
    fallback_resolved = False  # 时间窗 fallback 是否已尝试过
    first_attempt_done = False  # 第一次拉取是否已经做过
    empty_retries_since_fetch = 0  # 当前轮次中空 output 连续重试次数

    def _is_not_found_error(err: str) -> bool:
        e = err.lower()
        return (
            "资源不存在" in err
            or "not found" in e
            or "404" in e
        )

    for attempt in range(max_attempts):
        # 第一次拉取前先 sleep 5min（让 Langfuse 落库）
        if not first_attempt_done:
            log(
                "INFO",
                "INFERENCE",
                "Langfuse 兜底：响应结束后等待 5min 让 trace 落库",
                trace_id=actual_trace_id,
                anchor_ts=str(anchor_ts) if anchor_ts else None,
            )
            await asyncio.sleep(FIRST_DELAY_SEC)
            first_attempt_done = True

        fetched_ok = False
        output_empty = False
        try:
            trace = await client.get_trace(actual_trace_id)
            fetched_ok = True
            text = _render_langfuse_output_as_text(trace.get("output"))
            if text:
                if attempt > 0 or not first_attempt_done:
                    log(
                        "INFO",
                        "INFERENCE",
                        "Langfuse 兜底命中",
                        trace_id=actual_trace_id,
                        attempts=attempt + 1,
                        answer_length=len(text),
                    )
                return text
            output_empty = True
        except Exception as e:
            err = str(e)
            # trace_id 资源不存在 → 时间窗 fallback 找一条最接近的业务 trace
            if (
                not fallback_resolved
                and anchor_ts is not None
                and _is_not_found_error(err)
            ):
                log(
                    "INFO",
                    "INFERENCE",
                    "trace_id 查不到，启用时间窗 fallback",
                    trace_id=actual_trace_id,
                    anchor_ts=str(anchor_ts),
                    attempt=attempt + 1,
                )
                try:
                    from services.langfuse_trace_resolver import (
                        find_trace_by_time_window,
                    )
                    t = await find_trace_by_time_window(
                        client=client,
                        anchor_ts=anchor_ts,
                        project_label=langfuse_project or "",
                    )
                    if t:
                        new_tid = t.get("id")
                        if new_tid and new_tid != actual_trace_id:
                            log(
                                "INFO",
                                "INFERENCE",
                                "时间窗 fallback 命中",
                                original_trace_id=actual_trace_id,
                                resolved_trace_id=new_tid,
                                trace_name=t.get("name"),
                            )
                            actual_trace_id = new_tid
                            fallback_resolved = True
                            # 立即尝试拉这条新 trace 的详情
                            continue
                except Exception as e2:
                    log(
                        "WARN",
                        "INFERENCE",
                        "时间窗 fallback 失败",
                        error=str(e2),
                    )
                fallback_resolved = True  # 标记已尝试过，避免反复尝试
            log(
                "WARN",
                "INFERENCE",
                "Langfuse 兜底拉取失败",
                trace_id=actual_trace_id,
                attempt=attempt + 1,
                error=err,
            )

        if attempt >= max_attempts - 1:
            return None

        # 拉到但 output 空：首次 30s 再试（避免 10min 纯空等），
        # 仍空则 5min 后重试（等 Langfuse 异步落库）
        # 拉取失败 / fallback 未找到：30s 轮询
        if fetched_ok and output_empty:
            empty_retries_since_fetch += 1
            if empty_retries_since_fetch == 1:
                log(
                    "INFO",
                    "INFERENCE",
                    "Langfuse 兜底：拉到但 output 为空，30s 后再试",
                    trace_id=actual_trace_id,
                    attempt=attempt + 1,
                )
                await asyncio.sleep(POLL_INTERVAL_SEC)
            else:
                log(
                    "INFO",
                    "INFERENCE",
                    "Langfuse 兜底：拉到但 output 仍为空，5min 后再试",
                    trace_id=actual_trace_id,
                    attempt=attempt + 1,
                )
                await asyncio.sleep(FIRST_DELAY_SEC)
        else:
            await asyncio.sleep(POLL_INTERVAL_SEC)
    return None


def _auto_fill_question(payload: dict, question: str):
    """
    智能填充：如果 payload 中常见的问题字段值为空字符串，
    自动用 question 内容填充。
    
    常见字段名：message, content, query, question, prompt, text, input
    """
    if not question:
        return
    
    # 一级字段检测（request 为 onto-agent /agentic/runs/sync 必填字段）
    common_fields = [
        "request", "user_query", "message", "content", "query",
        "question", "prompt", "text", "input",
    ]
    for field in common_fields:
        if field in payload and _is_empty_or_placeholder(payload[field]):
            payload[field] = question
            log("INFO", "INFERENCE", f"自动填充字段 '{field}' 为问题内容", question_preview=question[:50])
    
    # 检测 messages 数组中的 content 字段（OpenAI 格式）
    if "messages" in payload and isinstance(payload["messages"], list):
        for msg in payload["messages"]:
            if isinstance(msg, dict) and "content" in msg and msg["content"] == "":
                msg["content"] = question
                log("INFO", "INFERENCE", "自动填充 messages[].content 为问题内容", question_preview=question[:50])


def _serialize_conversation_history(history: list, current_input: str) -> str:
    """把多轮对话历史 + 当前输入序列化为单字符串(给单轮推理接口用)。

    onto-agent 的 /agentic/runs/sync 端点 request 字段是单字符串,
    多轮历史需拼成带角色标记的文本,LLM 能理解上下文。

    Args:
        history: [(role, content), ...], role∈{user,assistant}
        current_input: 当前用户输入

    Returns:
        序列化后的对话文本;无历史时直接返回 current_input
    """
    if not history:
        return current_input
    parts = []
    for role, content in history:
        marker = "用户" if str(role).lower() == "user" else "助手"
        parts.append(f"{marker}: {content}")
    parts.append(f"用户: {current_input}")
    return "\n\n".join(parts)


class InferenceService:
    """推理服务 - 调用外部推理接口获取模型答案"""
    
    @staticmethod
    async def call_inference(
        question: str,
        config: Dict[str, Any],
        ground_truth: Optional[str] = None,
        trace_context: Optional["TraceContext"] = None,
        agent_run_id: Optional[str] = None,
        langfuse_project: Optional[str] = None,
        logical_agent_id: Optional[str] = None,
        agent_version: Optional[str] = None,
        task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        调用推理接口获取单个问题的模型答案
        
        Args:
            question: 问题文本
            config: 推理配置（包含 url, endpoint, headers, auth, timeout 等）
            ground_truth: 真实答案（可选，用于传递给推理接口）
            
        Returns:
            包含 model_answer 和可能的 error 的字典
        """
        if trace_context is not None:
            from services.langfuse_tracing import trace_agent_inference
            return await trace_agent_inference(
                question=question,
                ctx=trace_context,
                call_fn=lambda: InferenceService.call_inference(
                    question,
                    config,
                    ground_truth,
                    trace_context=None,
                    agent_run_id=agent_run_id,
                    langfuse_project=langfuse_project,
                    logical_agent_id=logical_agent_id,
                    agent_version=agent_version,
                    task_id=task_id,
                    agent_id=agent_id,
                ),
            )

        base_url = config.get("url", "").rstrip("/")
        base_url = resolve_agent_base_url(base_url)
        if not base_url:
            log("ERROR", "INFERENCE", "推理接口地址未配置")
            return {"model_answer": "", "error": "推理接口地址未配置"}
        
        headers = config.get("headers", {})
        auth = config.get("auth", {})
        timeout = config.get("timeout", 3600)
        endpoint = config.get("endpoint", "infer")
        endpoint_clean = endpoint.lstrip("/")
        payload_template = _normalize_payload_template(config.get("payload_template"))
        
        # 支持模板变量替换
        # ${question} -> 问题文本
        # ${ground_truth} -> 真实答案
        template_vars = {
            "${question}": question or "",
            "${ground_truth}": ground_truth or "",
        }
        
        if payload_template:
            payload = {}
            for key, value in payload_template.items():
                if isinstance(value, str):
                    replaced = value
                    for var_name, var_value in template_vars.items():
                        replaced = replaced.replace(var_name, var_value)
                    payload[key] = replaced
                elif isinstance(value, list):
                    payload[key] = _replace_in_list(value, template_vars)
                elif isinstance(value, dict):
                    payload[key] = _replace_in_dict(value, template_vars)
                else:
                    payload[key] = value
        else:
            if endpoint_clean in ("agentic/runs/sync", "agentic/runs"):
                payload = {
                    "request": question,
                    "context": {
                        "agent_mode": "thinking"
                    }
                }
            else:
                payload = {
                    "user_query": question
                }
        
        _auto_fill_question(payload, question)
        _ensure_agentic_request(payload, question, endpoint)
        _inject_eval_sync_context(
            payload,
            endpoint=endpoint_clean,
            agent_run_id=agent_run_id,
            langfuse_project=langfuse_project,
            logical_agent_id=logical_agent_id,
            agent_version=agent_version,
            task_id=task_id,
        )
        
        try:
            connector = aiohttp.TCPConnector(limit=10)
            # onto-agent /agentic/runs/sync 是同步（非流式）接口，
            # 全部工具执行完后才返回 JSON。工具链可能耗时 30 分钟以上，期间 HTTP
            # 静默无数据。使用 inference_config.timeout（默认 1800s）作为 total
            # 兜底超时，connect=30s 防止连接不到。timeout=None 时不限总时长。
            client_timeout = aiohttp.ClientTimeout(total=timeout or None, connect=30)
            
            request_headers = headers.copy()

            # 处理认证
            current_token = None
            if auth.get("type") == "bearer":
                current_token = auth.get('token', '')
                request_headers["Authorization"] = f"Bearer {current_token}"
            elif auth.get("type") == "api_key":
                header_name = auth.get("header_name", "X-API-Key")
                request_headers[header_name] = auth.get("api_key", "")
            elif auth.get("type") == "basic_auth":
                # 使用登录获取的 token 进行认证
                current_token = auth.get('token', '')
                if current_token:
                    # 检查 token 是否过期
                    if _is_token_expired(current_token):
                        log("WARNING", "INFERENCE", "Token 已过期，尝试自动刷新")
                        # 尝试使用用户名和密码刷新 token
                        new_token = await _refresh_token(
                            base_url,
                            auth.get('username', ''),
                            auth.get('password', '')
                        )
                        if new_token:
                            current_token = new_token
                            log("INFO", "INFERENCE", "使用刷新后的 Token 进行请求")
                            # 回写数据库，避免后续请求重复登录
                            if agent_id:
                                await _persist_refreshed_token(agent_id, new_token)
                        else:
                            log("ERROR", "INFERENCE", "Token 刷新失败，将使用过期 Token 重试")

                    request_headers["Authorization"] = f"Bearer {current_token}"

            # 添加工作空间头
            workspace_id = config.get("workspace_id")
            if workspace_id:
                request_headers["X-Workspace-Id"] = workspace_id

            url = f"{base_url}/{endpoint_clean}"
            # ====== 异步 /agentic/runs 适配 (create + poll) ======
            if endpoint_clean in ("agentic/runs",):
                log("INFO", "INFERENCE", "使用异步模式(agentic/runs create+poll)", url=url)
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(limit=10),
                    timeout=aiohttp.ClientTimeout(total=timeout or None, connect=30),
                ) as async_session:
                    async_result = await _call_agentic_runs_async(
                        async_session,
                        url=url,
                        payload=payload,
                        request_headers=request_headers,
                        timeout=timeout,
                    )
                if async_result.get("error"):
                    return {"model_answer": "", "error": async_result["error"]}
                result = async_result
                model_answer = (
                    result.get("final_answer", "") or
                    result.get("output", "") or
                    result.get("response", "") or
                    str(result.get("result", "") or "") or
                    result.get("text", "") or
                    result.get("answer", "") or
                    result.get("content", "") or
                    result.get("message", "")
                )
                inner_result = result.get("result")
                if isinstance(inner_result, dict):
                    model_answer = (
                        inner_result.get("final_answer", "") or
                        inner_result.get("output", "") or
                        inner_result.get("answer", "") or
                        inner_result.get("content", "") or
                        model_answer
                    )
                elif isinstance(inner_result, str) and inner_result.strip():
                    model_answer = inner_result
                from services.inference_metrics import (
                    extract_inference_runtime_fields,
                    extract_inference_token_breakdown,
                )
                from services.inference_model_display import (
                    parse_execution_mode_from_agent_response,
                    parse_inference_model_from_agent_response,
                )
                from services.onto_agent_trace_id import extract_onto_agent_run_fields
                agent_duration_ms, tokens_total = extract_inference_runtime_fields(result)
                tokens_llm, tokens_tool, usage = extract_inference_token_breakdown(result)
                onto_fields = extract_onto_agent_run_fields(result)
                response_model = parse_inference_model_from_agent_response(result)
                response_mode = parse_execution_mode_from_agent_response(result)
                if not isinstance(model_answer, str):
                    import json as _json
                    model_answer = _json.dumps(model_answer, ensure_ascii=False) if model_answer else ""
                log("INFO", "INFERENCE", "异步推理完成",
                    url=url, answer_preview=model_answer[:100] if model_answer else "",
                    answer_length=len(model_answer), duration_ms=agent_duration_ms,
                    tokens_total=tokens_total,
                    onto_agent_run_id=onto_fields.get("onto_agent_run_id"),
                    trace_id=onto_fields.get("trace_id"),
                )
                return {
                    "model_answer": model_answer,
                    "error": None,
                    "duration_ms": agent_duration_ms,
                    "tokens_total": tokens_total,
                    "tokens_llm": tokens_llm,
                    "tokens_tool": tokens_tool,
                    "usage": usage,
                    "trace_id": onto_fields.get("trace_id"),
                    "onto_agent_run_id": onto_fields.get("onto_agent_run_id"),
                    "onto_agent_session_id": onto_fields.get("session_id"),
                    "inference_model": response_model,
                    "execution_mode": response_mode,
                }


            log("INFO", "INFERENCE", "推理请求",
                url=url,
                question=question,
                payload=payload,
                headers=request_headers,
                timeout=timeout)

            async with aiohttp.ClientSession(connector=connector, timeout=client_timeout) as session:
                async with session.post(url, json=payload, headers=request_headers) as response:
                    # 如果是 401 错误且是 basic_auth 类型，尝试刷新 token 后重试一次
                    if response.status == 401 and auth.get("type") == "basic_auth":
                        log("WARNING", "INFERENCE", "收到 401 错误，尝试刷新 Token 后重试")

                        new_token = await _refresh_token(
                            base_url,
                            auth.get('username', ''),
                            auth.get('password', '')
                        )

                        if new_token:
                            request_headers["Authorization"] = f"Bearer {new_token}"
                            log("INFO", "INFERENCE", "使用新 Token 重试请求")
                            # 回写数据库，避免后续请求重复登录
                            if agent_id:
                                await _persist_refreshed_token(agent_id, new_token)

                            async with session.post(url, json=payload, headers=request_headers) as retry_response:
                                if retry_response.status == 200:
                                    response = retry_response
                                else:
                                    error_text = await retry_response.text()
                                    log("ERROR", "INFERENCE", "重试后仍然失败",
                                        status_code=retry_response.status, error=error_text)
                                    return {"model_answer": "", "error": f"推理接口返回状态码 {retry_response.status}: {error_text}"}
                        else:
                            error_text = await response.text()
                            log("ERROR", "INFERENCE", "Token 刷新失败", error=error_text)
                            return {"model_answer": "", "error": f"推理接口返回状态码 401: Token 过期且刷新失败 - {error_text}"}

                    if response.status != 200:
                        error_text = await response.text()
                        log("ERROR", "INFERENCE", "推理接口返回错误",
                            url=url,
                            status_code=response.status,
                            request_payload=payload,
                            response_text=error_text)
                        return {"model_answer": "", "error": f"推理接口返回状态码 {response.status}: {error_text}"}
                    
                    response_text = await response.text()
                    
                    # 尝试解析 JSON，兼容非标准 Content-Type 和非 JSON 响应
                    try:
                        result = json.loads(response_text) if response_text.strip() else {}
                    except json.JSONDecodeError:
                        # 响应不是 JSON，直接作为文本答案
                        log("INFO", "INFERENCE", "推理接口返回非JSON响应，作为纯文本处理", 
                            url=url, response_preview=response_text[:200])
                        result = {"text": response_text}
                    
                    # 尝试从多个可能的字段获取答案
                    model_answer = (
                        result.get("final_answer", "") or
                        result.get("output", "") or
                        result.get("response", "") or
                        result.get("result", "") or
                        result.get("text", "") or
                        result.get("answer", "") or
                        result.get("content", "") or
                        result.get("message", "")
                    )

                    from services.inference_metrics import (
                        extract_inference_runtime_fields,
                        extract_inference_token_breakdown,
                    )
                    from services.inference_model_display import (
                        parse_execution_mode_from_agent_response,
                        parse_inference_model_from_agent_response,
                    )
                    from services.onto_agent_trace_id import extract_onto_agent_run_fields

                    agent_duration_ms, tokens_total = extract_inference_runtime_fields(result)
                    tokens_llm, tokens_tool, usage = extract_inference_token_breakdown(result)
                    onto_fields = extract_onto_agent_run_fields(result)
                    response_model = parse_inference_model_from_agent_response(result)
                    response_mode = parse_execution_mode_from_agent_response(result)

                    # 兜底：agent 同步接口返回的 final_answer 为空时（已知 onto-agent
                    # 偶发场景），从 Langfuse 拉 trace 真实 output 补全。考虑 Langfuse
                    # 上报时延，轮询等待最长 ~5min。响应刚回来时记锚点（最接近
                    # on-agent 把 trace 写入 Langfuse 的时间点），用于时间窗 fallback。
                    response_received_at = datetime.now(timezone.utc)
                    if not (isinstance(model_answer, str) and model_answer.strip()):
                        fallback_trace_id = onto_fields.get("trace_id")
                        if fallback_trace_id and langfuse_project:
                            try:
                                fallback_answer = await _fetch_answer_from_langfuse_with_retry(
                                    trace_id=fallback_trace_id,
                                    langfuse_project=langfuse_project,
                                    anchor_ts=response_received_at,
                                )
                            except Exception as e:
                                log(
                                    "WARN",
                                    "INFERENCE",
                                    "Langfuse 兜底异常",
                                    trace_id=fallback_trace_id,
                                    error=str(e),
                                )
                                fallback_answer = None
                            if fallback_answer:
                                log(
                                    "INFO",
                                    "INFERENCE",
                                    "final_answer 为空，已从 Langfuse 兜底补全",
                                    trace_id=fallback_trace_id,
                                    answer_length=len(fallback_answer),
                                )
                                model_answer = fallback_answer

                    # 防御性序列化：model_answer 必须是 str，否则 evaluation_results
                    # .agent_answer（VARCHAR）写入会因 dict/list 报 asyncpg.DataError。
                    # 覆盖场景：
                    # 1) on-agent 返回 final_answer 是非空 dict / list（or 链被截留）
                    # 2) Langfuse 兜底因 trace_id / langfuse_project 缺失而未触发
                    # 3) 兜底 5min 内未命中（max_attempts 用尽）
                    # 渲染策略：
                    #   - dict: 先用 _render_langfuse_output_as_text 尝试 markdown 渲染
                    #   - 渲染失败 / 非 dict / list: json.dumps（ensure_ascii=False）
                    if not isinstance(model_answer, str):
                        if model_answer is None or model_answer == "":
                            model_answer = ""
                        elif isinstance(model_answer, (dict, list)):
                            try:
                                rendered = _render_langfuse_output_as_text(model_answer)
                            except Exception:
                                rendered = ""
                            if rendered:
                                model_answer = rendered
                                log(
                                    "INFO",
                                    "INFERENCE",
                                    "model_answer 非字符串，已渲染 dict 为文本",
                                    original_type=type(model_answer).__name__,
                                    answer_length=len(rendered),
                                )
                            else:
                                model_answer = json.dumps(
                                    model_answer, ensure_ascii=False
                                )
                                log(
                                    "WARN",
                                    "INFERENCE",
                                    "model_answer 非字符串且无法渲染，fallback 到 JSON 序列化",
                                    original_type=type(model_answer).__name__,
                                    answer_length=len(model_answer),
                                )
                        else:
                            model_answer = str(model_answer)

                    log(
                        "INFO",
                        "INFERENCE",
                        "推理响应",
                        url=url,
                        question=question,
                        response=result,
                        answer=model_answer,
                        answer_length=len(model_answer),
                        duration_ms=agent_duration_ms,
                        tokens_total=tokens_total,
                        onto_agent_run_id=onto_fields.get("onto_agent_run_id"),
                        trace_id=onto_fields.get("trace_id"),
                    )
                    return {
                        "model_answer": model_answer,
                        "error": None,
                        "duration_ms": agent_duration_ms,
                        "tokens_total": tokens_total,
                        "tokens_llm": tokens_llm,
                        "tokens_tool": tokens_tool,
                        "usage": usage,
                        "trace_id": onto_fields.get("trace_id"),
                        "onto_agent_run_id": onto_fields.get("onto_agent_run_id"),
                        "onto_agent_session_id": onto_fields.get("session_id"),
                        "inference_model": response_model,
                        "execution_mode": response_mode,
                    }
                    
        except asyncio.TimeoutError:
            log("ERROR", "INFERENCE", "推理接口调用超时", url=url, timeout=timeout)
            return {"model_answer": "", "error": "推理接口调用超时"}
        except aiohttp.ClientError as e:
            log("ERROR", "INFERENCE", "网络请求失败", url=url, error=str(e))
            return {"model_answer": "", "error": f"网络请求失败: {str(e)}"}
        except Exception as e:
            log("ERROR", "INFERENCE", "推理接口调用异常", url=url, error=str(e))
            return {"model_answer": "", "error": f"推理接口调用异常: {str(e)}"}

    @staticmethod
    async def call_inference_multiturn(
        user_input: str,
        history: list,
        config: Dict[str, Any],
        ground_truth: Optional[str] = None,
        trace_context: Optional["TraceContext"] = None,
        agent_run_id: Optional[str] = None,
        langfuse_project: Optional[str] = None,
        logical_agent_id: Optional[str] = None,
        agent_version: Optional[str] = None,
        task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """多轮推理:把 history + 当前 user_input 序列化为单字符串,调 call_inference。

        用于会话模拟器(Phase 4):ConversationSimulator 每生成一个 user 回合,
        调本方法把对话历史 + 当前输入发给被测 Agent,取回 assistant 回复。

        Args:
            user_input: 当前用户输入(模拟器生成的最新 user 回合)
            history: 之前轮次 [(role, content), ...],role∈{user,assistant}
            config/ground_truth/...: 同 call_inference
        """
        conversation_text = _serialize_conversation_history(history, user_input)
        return await InferenceService.call_inference(
            question=conversation_text,
            config=config,
            ground_truth=ground_truth,
            trace_context=trace_context,
            agent_run_id=agent_run_id,
            langfuse_project=langfuse_project,
            logical_agent_id=logical_agent_id,
            agent_version=agent_version,
            task_id=task_id,
            agent_id=agent_id,
        )

    @staticmethod
    async def batch_call_inference(
        items: list,
        config: Dict[str, Any],
        on_progress: Optional[callable] = None
    ) -> list:
        """
        批量调用推理接口
        
        Args:
            items: 数据项列表，每项应包含 question 字段
            config: 推理配置
            on_progress: 进度回调函数 (current, total)
            
        Returns:
            处理后的数据项列表，每项添加 model_answer 字段
        """
        request_delay = config.get("request_delay", 0.1)
        processed_items = []
        total = len(items)
        
        for idx, item in enumerate(items):
            question = item.get("question", "") or item.get("input", "")
            ground_truth = item.get("ground_truth", "") or item.get("expected_output", "")
            
            result = await InferenceService.call_inference(question, config, ground_truth)
            
            new_item = {
                **item,
                "question": question,
                "ground_truth": ground_truth,
                "model_answer": result.get("model_answer", ""),
            }
            
            if result.get("error"):
                new_item["inference_error"] = result["error"]
            
            processed_items.append(new_item)
            
            # 进度回调
            if on_progress:
                await on_progress(idx + 1, total)
            
            # 请求间隔
            if idx < total - 1 and request_delay > 0:
                await asyncio.sleep(request_delay)
        
        return processed_items


# ====== 异步 /agentic/runs 适配 (create + poll) ======
_TERMINAL_STATUSES = frozenset({
    "completed", "succeeded", "done", "failed", "cancelled", "error",
})


async def _call_agentic_runs_async(
    session,
    *,
    url: str,
    payload: dict,
    request_headers: dict,
    timeout: int,
    agent_run_id=None,
) -> dict:
    """异步调用 /agentic/runs: POST创建run -> GET轮询直到完成。
    替代 sync_adapter 的功能，直接在 eval 平台内完成 create+poll。
    """
    import uuid as _uuid

    create_body = dict(payload)
    if not create_body.get("request"):
        create_body["request"] = payload.get("request", "")
    if "session_id" not in create_body:
        create_body["session_id"] = f"session_{_uuid.uuid4().hex[:12]}"
    if "include_trace" not in create_body:
        create_body["include_trace"] = False

    # 添加 Idempotency-Key header (backend必需)
    create_headers = dict(request_headers)
    idempotency_key = f"eval-{_uuid.uuid4().hex[:16]}"
    create_headers["Idempotency-Key"] = idempotency_key

    log("INFO", "INFERENCE", "异步创建run", url=url, idempotency_key=idempotency_key)

    async with session.post(
        url,
        json=create_body,
        headers=create_headers,
        timeout=aiohttp.ClientTimeout(total=60, connect=15),
    ) as create_resp:
        create_text = await create_resp.text()
        if create_resp.status not in (200, 201, 202):
            log("ERROR", "INFERENCE", "创建run失败",
                status_code=create_resp.status, error=create_text[:500])
            return {"error": f"创建run失败({create_resp.status}): {create_text[:200]}"}
        try:
            create_result = json.loads(create_text) if create_text.strip() else {}
        except json.JSONDecodeError:
            create_result = {"text": create_text}

    if create_result.get("result") is not None:
        log("INFO", "INFERENCE", "run在创建时已完成")
        return create_result

    run_id = create_result.get("run_id")
    if not run_id:
        log("WARNING", "INFERENCE", "创建run无run_id", result_keys=list(create_result.keys()))
        return create_result

    log("INFO", "INFERENCE", "run已创建", run_id=run_id, status=create_result.get("status"))

    poll_url = f"{url}/{run_id}"
    poll_interval = 2
    max_poll_time = timeout or 1800
    start_time = time.time()
    poll_count = 0

    while time.time() - start_time < max_poll_time:
        poll_count += 1
        await asyncio.sleep(poll_interval)

        try:
            async with session.get(
                poll_url,
                headers=request_headers,
                timeout=aiohttp.ClientTimeout(total=30, connect=10),
            ) as poll_resp:
                poll_text = await poll_resp.text()
                if poll_resp.status != 200:
                    log("WARNING", "INFERENCE", "轮询失败",
                        status_code=poll_resp.status, poll_count=poll_count)
                    continue
                try:
                    poll_result = json.loads(poll_text) if poll_text.strip() else {}
                except json.JSONDecodeError:
                    poll_result = {"text": poll_text}

                status = poll_result.get("status", "")
                has_result = poll_result.get("result") is not None

                if poll_count <= 3 or poll_count % 30 == 0:
                    log("INFO", "INFERENCE", "轮询run",
                        run_id=run_id, poll_count=poll_count,
                        status=status, has_result=has_result)

                if status in _TERMINAL_STATUSES or has_result:
                    log("INFO", "INFERENCE", "run已完成",
                        run_id=run_id, status=status,
                        poll_count=poll_count,
                        elapsed=int(time.time() - start_time))
                    return poll_result

        except Exception as e:
            log("WARNING", "INFERENCE", "轮询异常",
                run_id=run_id, error=str(e), poll_count=poll_count)
            await asyncio.sleep(3)

    log("ERROR", "INFERENCE", "run轮询超时",
        run_id=run_id, timeout=max_poll_time)
    return {"error": f"run轮询超时({max_poll_time}s)", "run_id": run_id}
