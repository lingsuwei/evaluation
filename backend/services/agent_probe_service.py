"""Agent 推理配置注册前 sync 健康预检（H-03）。"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from logger import log
from services.inference_service import _is_token_expired, _refresh_token
from utils.agent_url import resolve_agent_base_url

PROBE_SYNC_TIMEOUT_SEC = 120
PROBE_HTTP_TIMEOUT_SEC = 15


def _check(
    name: str,
    ok: bool,
    message: str,
    *,
    severity: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {"name": name, "ok": ok, "message": message}
    if severity:
        row["severity"] = severity
    row.update(extra)
    return row


def _warn_check(name: str, message: str, **extra: Any) -> Dict[str, Any]:
    """非阻断警告项：ok 仍为 True，前端以 warning 样式展示。"""
    return _check(name, True, message, severity="warning", **extra)


def _finalize(
    checks: List[Dict[str, Any]],
    resolved_url: Optional[str],
    suggestions: List[str],
    *,
    workspace_id: Optional[str] = None,
    langfuse_project: Optional[str] = None,
    langfuse_project_status: Optional[str] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "ok": all(c.get("ok") for c in checks),
        "has_warnings": any(c.get("severity") == "warning" for c in checks),
        "resolved_url": resolved_url,
        "workspace_id": workspace_id,
        "checks": checks,
        "suggestions": suggestions,
    }
    if langfuse_project:
        result["langfuse_project"] = langfuse_project
    if langfuse_project_status:
        result["langfuse_project_status"] = langfuse_project_status
    return result


def _preview_public_key(public_key: str) -> str:
    # public_key 按 Langfuse 设计本就公开，完整返回以便用户核对/复制，
    # 不再做前 16 位截断掩码。
    return (public_key or "").strip()


def resolve_langfuse_project_from_system_config(
    system_config: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """从 onto-agent 观测配置解析 Langfuse 项目绑定（不抛错）。

    兼容两种返回结构：
    - 旧：{"observability": {"enabled": ..., "langfuse": {...}}}（系统级 system-config）
    - 新：{"enabled": ..., "langfuse": {...}}（工作空间级 observability-config）
    """
    if not isinstance(system_config, dict):
        return {"status": "fetch_failed", "langfuse_project": None}

    # 新接口直接返回观测配置；旧接口嵌套在 observability 下
    obs = system_config.get("observability") if "observability" in system_config else system_config
    if not isinstance(obs, dict):
        return {"status": "not_configured", "langfuse_project": None}
    if not obs.get("enabled"):
        return {"status": "not_configured", "langfuse_project": None}

    lf = obs.get("langfuse") if isinstance(obs.get("langfuse"), dict) else {}
    public_key = str(lf.get("public_key") or "").strip()
    has_secret = bool(lf.get("has_secret_key"))
    if not public_key or not has_secret:
        return {"status": "not_configured", "langfuse_project": None}

    from config import LANGFUSE_ENABLED
    from services.langfuse_project_registry import find_project_by_public_key

    if not LANGFUSE_ENABLED:
        return {
            "status": "eval_disabled",
            "langfuse_project": None,
            "public_key_preview": _preview_public_key(public_key),
        }

    matched = find_project_by_public_key(public_key)
    if matched:
        # 校验 host 一致性：agent 写 trace 的 Langfuse 地址须与 eval 注册表一致
        agent_host = str(lf.get("host") or "").strip().rstrip("/")
        eval_host = (matched.host or "").strip().rstrip("/")
        if agent_host and eval_host and agent_host.lower() != eval_host.lower():
            return {
                "status": "host_mismatch",
                "langfuse_project": None,
                "public_key_preview": _preview_public_key(public_key),
                "agent_host": agent_host,
                "eval_host": eval_host,
            }
        return {
            "status": "matched",
            "langfuse_project": matched.id,
            "project_name": matched.name,
            "public_key_preview": _preview_public_key(public_key),
        }

    return {
        "status": "not_registered",
        "langfuse_project": None,
        "public_key_preview": _preview_public_key(public_key),
    }


async def _fetch_workspace_observability_config(
    session: aiohttp.ClientSession,
    base_url: str,
    workspace_id: str,
    headers: Dict[str, str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """获取 onto-agent 工作空间级观测配置。

    onto-agent 已将 observability 从系统级 /api/system-config 迁移至
    工作空间级 /agentic/workspaces/<workspace_id>/observability-config，
    系统级配置不再返回 observability section。
    """
    url = f"{base_url.rstrip('/')}/agentic/workspaces/{workspace_id}/observability-config"
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 404:
                return None, "not_onto_agent"
            if resp.status in (401, 403):
                return None, "auth_denied"
            if resp.status != 200:
                text = (await resp.text())[:120]
                return None, f"http_{resp.status}:{text}"
            data = await resp.json()
            return data if isinstance(data, dict) else None, None
    except aiohttp.ClientError as exc:
        return None, f"network:{exc}"
    except Exception as exc:
        return None, f"error:{exc}"


async def _fetch_agent_system_config(
    session: aiohttp.ClientSession,
    base_url: str,
    headers: Dict[str, str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """获取 onto-agent 系统级配置（旧接口，作为工作空间级接口的回退）。

    旧版 onto-agent 在 /api/system-config 的 observability section 暴露 Langfuse 配置；
    新版已迁移至工作空间级 /agentic/workspaces/<id>/observability-config。
    """
    url = f"{base_url.rstrip('/')}/api/system-config"
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 404:
                return None, "not_onto_agent"
            if resp.status in (401, 403):
                return None, "auth_denied"
            if resp.status != 200:
                text = (await resp.text())[:120]
                return None, f"http_{resp.status}:{text}"
            data = await resp.json()
            return data if isinstance(data, dict) else None, None
    except aiohttp.ClientError as exc:
        return None, f"network:{exc}"
    except Exception as exc:
        return None, f"error:{exc}"


async def _fetch_observability_config_with_fallback(
    session: aiohttp.ClientSession,
    base_url: str,
    workspace_id: str,
    headers: Dict[str, str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """先试工作空间级 observability-config，404 时回退系统级 system-config。

    兼容新旧 onto-agent：新版只暴露工作空间级接口，旧版只在 system-config 带 observability。
    """
    obs_config, err = await _fetch_workspace_observability_config(
        session, base_url, workspace_id, headers
    )
    if obs_config is not None:
        return obs_config, None
    # 404 表示该 onto-agent 未提供工作空间级接口，回退到系统级 system-config
    if err == "not_onto_agent":
        return await _fetch_agent_system_config(session, base_url, headers)
    return None, err


def _append_langfuse_probe_check(
    checks: List[Dict[str, Any]],
    suggestions: List[str],
    resolved: Dict[str, Any],
) -> Optional[str]:
    """写入 langfuse 预检项（始终 ok=True，不阻断 Agent 注册）。"""
    status = resolved.get("status") or "fetch_failed"
    project = resolved.get("langfuse_project")
    preview = resolved.get("public_key_preview") or ""

    if status == "matched":
        name = resolved.get("project_name") or project
        checks.append(
            _check(
                "langfuse",
                True,
                f"已自动匹配 eval 项目「{name}」（{project}）",
                langfuse_project=project,
                langfuse_project_status=status,
            )
        )
        return project

    if status == "not_configured":
        checks.append(
            _warn_check(
                "langfuse",
                "Agent 未启用 Langfuse；创建 Agent 不受影响，Trace 对比/物化不可用",
                langfuse_project_status=status,
            )
        )
        return None

    if status == "not_registered":
        checks.append(
            _warn_check(
                "langfuse",
                f"Agent 已配置 Langfuse，但 public_key（{preview}）未在 eval 注册；创建不受影响",
                langfuse_project_status=status,
                public_key_preview=preview,
            )
        )
        suggestions.append(
            "请在 langfuse.projects.json 中添加与 Agent 相同的 public_key，以便 Trace 探索与物化"
        )
        return None

    if status == "host_mismatch":
        agent_host = resolved.get("agent_host", "?")
        eval_host = resolved.get("eval_host", "?")
        checks.append(
            _warn_check(
                "langfuse",
                f"Agent Langfuse Host（{agent_host}）与 eval 注册表（{eval_host}）不一致，Trace 无法互通",
                langfuse_project_status=status,
                public_key_preview=preview,
            )
        )
        suggestions.append(
            f"请将 Agent 的 LANGFUSE_HOST 改为 {eval_host}，或在 eval 注册表中添加对应 host 的项目"
        )
        return None

    if status == "eval_disabled":
        checks.append(
            _warn_check(
                "langfuse",
                "eval 未启用 Langfuse，Trace 相关能力不可用",
                langfuse_project_status=status,
            )
        )
        return None

    checks.append(
        _warn_check(
            "langfuse",
            "无法读取 Agent 观测配置，Langfuse 自动绑定已跳过",
            langfuse_project_status="fetch_failed",
        )
    )
    return None


async def resolve_langfuse_project_from_inference_config(
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """独立解析：供注册/更新时自动绑定 Langfuse 项目（不阻断）。"""
    raw_url = (config.get("url") or "").strip()
    if not raw_url:
        return {"status": "no_url", "langfuse_project": None}

    base_url = resolve_agent_base_url(raw_url.rstrip("/"))
    if not base_url:
        return {"status": "fetch_failed", "langfuse_project": None}

    checks: List[Dict[str, Any]] = []
    auth_headers, auth_ok = await _resolve_auth_headers(config, base_url, checks)
    if not auth_ok:
        return {"status": "fetch_failed", "langfuse_project": None}

    workspace_id = (config.get("workspace_id") or "").strip() or None
    headers = _request_headers(auth_headers, workspace_id)

    try:
        http_timeout = aiohttp.ClientTimeout(total=PROBE_HTTP_TIMEOUT_SEC)
        async with aiohttp.ClientSession(timeout=http_timeout) as session:
            if workspace_id:
                obs_config, _err = await _fetch_observability_config_with_fallback(
                    session, base_url, workspace_id, headers
                )
            else:
                # 无 workspace_id：新版工作空间级接口无法调用，回退系统级 system-config
                obs_config, _err = await _fetch_agent_system_config(
                    session, base_url, headers
                )
    except Exception as exc:
        log("WARNING", "AGENT_PROBE", "Langfuse 自动绑定读取观测配置失败", error=str(exc))
        return {"status": "fetch_failed", "langfuse_project": None}

    resolved = resolve_langfuse_project_from_system_config(obs_config)
    return resolved


async def _probe_langfuse_project(
    session: aiohttp.ClientSession,
    base_url: str,
    workspace_id: str,
    headers: Dict[str, str],
    checks: List[Dict[str, Any]],
    suggestions: List[str],
) -> Tuple[Optional[str], Optional[str]]:
    obs_config, _err = await _fetch_observability_config_with_fallback(
        session, base_url, workspace_id, headers
    )
    if obs_config is None:
        if _err == "not_onto_agent":
            checks.append(
                _warn_check(
                    "langfuse",
                    "非 onto-agent 或未暴露观测配置，Langfuse 自动绑定已跳过",
                    langfuse_project_status="fetch_failed",
                )
            )
        else:
            checks.append(
                _warn_check(
                    "langfuse",
                    "无法读取 Agent 观测配置，Langfuse 自动绑定已跳过",
                    langfuse_project_status="fetch_failed",
                )
            )
        return None, "fetch_failed"

    resolved = resolve_langfuse_project_from_system_config(obs_config)
    project = _append_langfuse_probe_check(checks, suggestions, resolved)
    return project, resolved.get("status")


async def _probe_langfuse_for_non_onto_agent(
    session: aiohttp.ClientSession,
    base_url: str,
    headers: Dict[str, str],
    checks: List[Dict[str, Any]],
    suggestions: List[str],
) -> Tuple[Optional[str], Optional[str]]:
    """非 onto-agent 端点的 Langfuse 探测：直接拉取 system-config 自动匹配。

    agent_swarm 等非 onto-agent 暴露 GET /api/system-config 返回 observability 配置，
    与 onto-agent 的 system-config 接口格式兼容，可直接复用匹配逻辑。
    """
    obs_config, _err = await _fetch_agent_system_config(session, base_url, headers)
    if obs_config is None:
        if _err == "not_onto_agent":
            checks.append(
                _warn_check(
                    "langfuse",
                    "Agent 未暴露观测配置接口，Langfuse 自动绑定已跳过",
                    langfuse_project_status="fetch_failed",
                )
            )
        else:
            checks.append(
                _warn_check(
                    "langfuse",
                    f"无法读取 Agent 观测配置（{_err}），Langfuse 自动绑定已跳过",
                    langfuse_project_status="fetch_failed",
                )
            )
        return None, "fetch_failed"

    resolved = resolve_langfuse_project_from_system_config(obs_config)
    project = _append_langfuse_probe_check(checks, suggestions, resolved)
    return project, resolved.get("status")


async def _resolve_auth_headers(
    config: Dict[str, Any],
    base_url: str,
    checks: List[Dict[str, Any]],
) -> Tuple[Dict[str, str], bool]:
    headers = dict(config.get("headers") or {})
    auth = config.get("auth") or {}
    auth_type = auth.get("type", "none")

    if auth_type == "none":
        checks.append(_check("auth", True, "未配置鉴权"))
        return headers, True

    if auth_type == "bearer":
        token = (auth.get("token") or "").strip()
        if not token:
            checks.append(_check("auth", False, "Bearer Token 未填写"))
            return headers, False
        headers["Authorization"] = f"Bearer {token}"
        checks.append(_check("auth", True, "已配置 Bearer Token"))
        return headers, True

    if auth_type == "api_key":
        api_key = (auth.get("api_key") or "").strip()
        if not api_key:
            checks.append(_check("auth", False, "API Key 未填写"))
            return headers, False
        header_name = auth.get("header_name") or "X-API-Key"
        headers[header_name] = api_key
        checks.append(_check("auth", True, f"已配置 API Key（{header_name}）"))
        return headers, True

    if auth_type == "basic_auth":
        token = (auth.get("token") or "").strip()
        username = auth.get("username") or ""
        password = auth.get("password") or ""
        if token and not _is_token_expired(token):
            headers["Authorization"] = f"Bearer {token}"
            checks.append(_check("auth", True, "使用已保存的登录 Token"))
            return headers, True
        if not username or not password:
            checks.append(_check("auth", False, "请先填写用户名密码并点击「登录」"))
            return headers, False
        new_token = await _refresh_token(base_url, username, password)
        if not new_token:
            checks.append(_check("auth", False, "登录失败，请检查用户名和密码"))
            return headers, False
        headers["Authorization"] = f"Bearer {new_token}"
        checks.append(_check("auth", True, "登录成功，已获取 Token"))
        return headers, True

    checks.append(_check("auth", False, f"不支持的鉴权类型: {auth_type}"))
    return headers, False


def _request_headers(headers: Dict[str, str], workspace_id: Optional[str]) -> Dict[str, str]:
    merged = dict(headers)
    if workspace_id:
        merged["X-Workspace-Id"] = workspace_id
    merged.setdefault("Content-Type", "application/json")
    return merged


async def probe_agent_inference_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    对 inference_config 执行 URL → 鉴权 → workspace → sync ping 预检。

    返回结构化结果，供 API 与注册阻断使用。
    """
    checks: List[Dict[str, Any]] = []
    suggestions: List[str] = []

    raw_url = (config.get("url") or "").strip()
    if not raw_url:
        checks.append(_check("url", False, "Agent 地址未填写"))
        return _finalize(checks, None, suggestions)

    base_url = resolve_agent_base_url(raw_url.rstrip("/"))
    if not base_url:
        checks.append(_check("url", False, "无法解析 Agent 地址"))
        return _finalize(checks, None, suggestions)

    if base_url != raw_url.rstrip("/"):
        checks.append(_check("url", True, f"地址已解析为 {base_url}", resolved_from=raw_url))
    else:
        checks.append(_check("url", True, f"地址: {base_url}"))

    auth_headers, auth_ok = await _resolve_auth_headers(config, base_url, checks)
    if not auth_ok:
        return _finalize(checks, base_url, suggestions)

    workspace_id = (config.get("workspace_id") or "").strip() or None
    ws_headers = _request_headers(auth_headers, workspace_id)
    ws_url = f"{base_url}/agentic/workspaces"
    langfuse_project: Optional[str] = None
    langfuse_project_status: Optional[str] = None

    # 判别是否 onto-agent 类端点（/agentic/*）。非 onto-agent（如 agent_swarm
    # /api/chat/sync）无 /agentic/workspaces 端点，workspace 检查自动跳过。
    is_onto_agent = "/agentic" in (config.get("endpoint") or "").lower() or "/agentic" in (config.get("url") or "").lower()

    try:
        http_timeout = aiohttp.ClientTimeout(total=PROBE_HTTP_TIMEOUT_SEC)
        async with aiohttp.ClientSession(timeout=http_timeout) as session:
            if is_onto_agent:
                # 先校验 workspace 并确定 workspace_id（工作空间级 observability 接口依赖它）
                async with session.get(ws_url, headers=ws_headers) as resp:
                    if resp.status == 401:
                        checks.append(_check("workspace", False, "认证失败（401），请检查 Token 或登录"))
                        return _finalize(checks, base_url, suggestions)
                    if resp.status == 403:
                        checks.append(_check("workspace", False, "权限不足（403）"))
                        return _finalize(checks, base_url, suggestions)
                    if resp.status != 200:
                        text = (await resp.text())[:200]
                        checks.append(
                            _check("workspace", False, f"获取工作空间失败 HTTP {resp.status}: {text}")
                        )
                        return _finalize(checks, base_url, suggestions)

                    data = await resp.json()
                    workspaces = data.get("workspaces") or []
                    ws_ids = [w.get("id") for w in workspaces if w.get("id")]
                    default_ws = data.get("default_workspace_id")

                    if workspace_id:
                        if workspace_id in ws_ids:
                            checks.append(
                                _check(
                                    "workspace",
                                    True,
                                    f"工作空间「{workspace_id}」存在",
                                    workspace_id=workspace_id,
                                )
                            )
                        else:
                            checks.append(
                                _check(
                                    "workspace",
                                    False,
                                    f"工作空间「{workspace_id}」不存在",
                                    available=ws_ids[:10],
                                )
                            )
                            suggestions.append("请在下拉列表中选择有效工作空间")
                            return _finalize(
                                checks, base_url, suggestions,
                                workspace_id=workspace_id,
                            )
                    elif default_ws:
                        workspace_id = default_ws
                        ws_headers = _request_headers(auth_headers, workspace_id)
                        checks.append(
                            _check(
                                "workspace",
                                True,
                                f"未指定工作空间，将使用默认 {default_ws}",
                                workspace_id=default_ws,
                            )
                        )
                    elif ws_ids:
                        checks.append(
                            _check("workspace", True, f"未指定工作空间（可用 {len(ws_ids)} 个）")
                        )
                    else:
                        checks.append(_check("workspace", False, "onto-agent 未返回任何工作空间"))
                        return _finalize(checks, base_url, suggestions)

                # workspace 确定后再探测 Langfuse（工作空间级 observability-config 接口）
                if workspace_id:
                    langfuse_project, langfuse_project_status = await _probe_langfuse_project(
                        session, base_url, workspace_id, ws_headers, checks, suggestions
                    )
            else:
                checks.append(_check("workspace", True, "非 onto-agent 端点，跳过 workspace 检查"))
                # 非 onto-agent 也尝试拉取 system-config 探测 Langfuse
                # （agent_swarm 等暴露 GET /api/system-config 返回 observability 配置）
                langfuse_project, langfuse_project_status = await _probe_langfuse_for_non_onto_agent(
                    session, base_url, ws_headers, checks, suggestions
                )

            for health_path in ("/health", "/api/health"):
                health_url = f"{base_url}{health_path}"
                try:
                    async with session.get(health_url, headers=ws_headers, timeout=aiohttp.ClientTimeout(total=5)) as health_resp:
                        if health_resp.status == 200:
                            checks.append(
                                _check(
                                    "sync",
                                    True,
                                    f"健康检查通过（{health_path}），跳过 sync ping",
                                    method="health",
                                )
                            )
                            return _finalize(
                                checks, base_url, suggestions,
                                workspace_id=workspace_id,
                                langfuse_project=langfuse_project,
                                langfuse_project_status=langfuse_project_status,
                            )
                except aiohttp.ClientError:
                    pass

            endpoint = (config.get("endpoint") or "/agentic/runs/sync").lstrip("/")
            sync_url = f"{base_url}/{endpoint}"
            payload = {
                "request": "ping",
                "context": {"agent_mode": "thinking"},
                "session_id": "onto_eval_probe",
            }
            sync_timeout_sec = min(int(config.get("timeout") or 1800), PROBE_SYNC_TIMEOUT_SEC)
            sync_timeout_sec = max(sync_timeout_sec, 30)
            sync_timeout = aiohttp.ClientTimeout(total=sync_timeout_sec)

            async with session.post(
                sync_url, json=payload, headers=ws_headers, timeout=sync_timeout
            ) as sync_resp:
                body_text = await sync_resp.text()
                if sync_resp.status == 404:
                    try:
                        err = json.loads(body_text)
                        msg = err.get("error") or err.get("detail") or body_text[:200]
                    except json.JSONDecodeError:
                        msg = body_text[:200]
                    checks.append(_check("sync", False, f"Sync 失败（404）: {msg}"))
                    return _finalize(
                        checks, base_url, suggestions,
                        workspace_id=workspace_id,
                        langfuse_project=langfuse_project,
                        langfuse_project_status=langfuse_project_status,
                    )
                if sync_resp.status == 401:
                    checks.append(_check("sync", False, "Sync 认证失败（401）"))
                    return _finalize(
                        checks, base_url, suggestions,
                        workspace_id=workspace_id,
                        langfuse_project=langfuse_project,
                        langfuse_project_status=langfuse_project_status,
                    )
                if sync_resp.status != 200:
                    checks.append(
                        _check(
                            "sync",
                            False,
                            f"Sync HTTP {sync_resp.status}: {body_text[:300]}",
                        )
                    )
                    return _finalize(
                        checks, base_url, suggestions,
                        workspace_id=workspace_id,
                        langfuse_project=langfuse_project,
                        langfuse_project_status=langfuse_project_status,
                    )

                try:
                    result = json.loads(body_text) if body_text.strip() else {}
                except json.JSONDecodeError:
                    checks.append(_check("sync", False, "Sync 响应非 JSON"))
                    return _finalize(
                        checks, base_url, suggestions,
                        workspace_id=workspace_id,
                        langfuse_project=langfuse_project,
                        langfuse_project_status=langfuse_project_status,
                    )

                usage = result.get("usage") if isinstance(result.get("usage"), dict) else None
                usage_preview = (
                    {k: usage[k] for k in list(usage.keys())[:8]} if usage else None
                )
                detail = f"Sync 成功（status={result.get('status', 'ok')}）"
                if usage_preview:
                    detail += "，已返回 usage 字段"
                else:
                    detail += "；未检测到 usage（Token 指标可能缺失）"
                    suggestions.append("确认 onto-agent 已部署含 RunUsageLedger 的版本")

                checks.append(
                    _check(
                        "sync",
                        True,
                        detail,
                        status_code=sync_resp.status,
                        has_usage=bool(usage),
                        usage_preview=usage_preview,
                        run_id=result.get("run_id"),
                    )
                )
    except aiohttp.ClientError as exc:
        checks.append(_check("connectivity", False, f"无法连接 Agent: {exc}"))
        if "localhost" in raw_url and "host.docker.internal" not in base_url:
            suggestions.append("Docker 部署时可尝试 http://host.docker.internal:5055")
        return _finalize(
            checks, base_url, suggestions,
            workspace_id=workspace_id,
            langfuse_project=langfuse_project,
            langfuse_project_status=langfuse_project_status,
        )
    except Exception as exc:
        log("ERROR", "AGENT_PROBE", "预检异常", error=str(exc))
        checks.append(_check("probe", False, f"预检异常: {exc}"))
        return _finalize(
            checks, base_url, suggestions,
            workspace_id=workspace_id,
            langfuse_project=langfuse_project,
            langfuse_project_status=langfuse_project_status,
        )

    return _finalize(
        checks, base_url, suggestions,
        workspace_id=workspace_id,
        langfuse_project=langfuse_project,
        langfuse_project_status=langfuse_project_status,
    )
