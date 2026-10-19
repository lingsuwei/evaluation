"""从 Agent inference_config 解析 onto-agent 工作空间展示名。"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from database import Agent


def parse_inference_config(agent: Optional[Agent]) -> Dict[str, Any]:
    if not agent or not agent.inference_config:
        return {}
    try:
        raw = json.loads(agent.inference_config)
        return raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def workspace_id_from_agent(agent: Optional[Agent]) -> Optional[str]:
    cfg = parse_inference_config(agent)
    wid = (cfg.get("workspace_id") or cfg.get("workspaceId") or "").strip()
    return wid or None


def workspace_name_from_agent(agent: Optional[Agent]) -> Optional[str]:
    """Agent 配置页保存的工作空间展示名（优先于实时拉取的 onto-agent 名称）。"""
    cfg = parse_inference_config(agent)
    name = (cfg.get("workspace_name") or cfg.get("workspaceName") or "").strip()
    return name or None


def workspace_cache_key(agent: Optional[Agent]) -> Optional[Tuple[str, str, str]]:
    """(base_url, api_token, workspace_id) 用于批量解析工作空间名称。"""
    if not agent:
        return None
    cfg = parse_inference_config(agent)
    wid = workspace_id_from_agent(agent)
    if not wid:
        return None
    auth = cfg.get("auth") if isinstance(cfg.get("auth"), dict) else {}
    base_url = (cfg.get("base_url") or cfg.get("baseUrl") or "").strip()
    token = ""
    if isinstance(auth, dict):
        token = (
            auth.get("api_token")
            or auth.get("apiToken")
            or auth.get("token")
            or ""
        ).strip()
        if not token and (auth.get("type") or "").strip().lower() == "bearer":
            token = (auth.get("bearer_token") or "").strip()
    if not base_url or not token:
        return None
    return (base_url, token, wid)


async def fetch_workspace_name_map(
  session_agents: list[Agent],
) -> Dict[str, str]:
    """
    按 (base_url, token) 分组各调一次 onto-agent /agentic/workspaces，
    返回 workspace_id -> name。
    """
    from collections import defaultdict

    import aiohttp

    from utils.agent_url import resolve_agent_base_url

    groups: Dict[Tuple[str, str], set[str]] = defaultdict(set)
    for ag in session_agents:
        key = workspace_cache_key(ag)
        if not key:
            continue
        base_url, token, wid = key
        groups[(base_url, token)].add(wid)

    name_map: Dict[str, str] = {}
    for (base_url, token), wids in groups.items():
        resolved = resolve_agent_base_url(base_url)
        url = f"{resolved.rstrip('/')}/agentic/workspaces"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    for ws in data.get("workspaces") or []:
                        ws_id = (ws.get("id") or "").strip()
                        ws_name = (ws.get("name") or "").strip()
                        if ws_id and ws_name:
                            name_map[ws_id] = ws_name
        except Exception:
            continue
    return name_map


def resolve_workspace_display(
    agent: Optional[Agent],
    name_map: Dict[str, str],
) -> Optional[str]:
    wid = workspace_id_from_agent(agent)
    if not wid:
        return workspace_name_from_agent(agent)
    stored_name = workspace_name_from_agent(agent)
    if stored_name:
        return stored_name
    if wid in name_map:
        return name_map[wid]
    lowered = wid.lower()
    for key, value in name_map.items():
        if key.lower() == lowered and value:
            return value
    # 未拉取到名称时展示 ID 短码，避免历史列全空
    return wid if len(wid) <= 24 else f"{wid[:20]}…"
