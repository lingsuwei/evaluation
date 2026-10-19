"""onto-eval AI 对话式配置 — 写操作执行器（阶段 2）。

集中处理 confirm 端点的写操作执行，复用 main.py 的 CRUD 逻辑。
所有函数统一模式：延迟 import main.py 辅助函数 → 执行 CRUD → catch 异常 →
返回 {ok: bool, message: str, ...}。不抛 HTTPException。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from logger import log


async def execute_confirmed_action(
    tool_name: str,
    tool_args: dict,
    session: AsyncSession,
    session_id: Optional[str] = None,
) -> dict:
    """执行已确认的写操作。

    Args:
        tool_name: 工具名（create_llm_model / update_agent / delete_eval_script 等；
                   launch_eval 已迁至 tools.py 自主执行，不再走确认流程）
        tool_args: LLM 传入的参数（已 JSON 解析）
        session: DB 会话
        session_id: 聊天会话 ID（保留参数，当前无 handler 需要）

    Returns:
        操作结果 dict，至少含 {ok: bool, message: str}
    """
    handlers = {
        "create_llm_model": _create_llm_model,
        "update_llm_model": _update_llm_model,
        "delete_llm_model": _delete_llm_model,
        "create_agent": _create_agent,
        "update_agent": _update_agent,
        "delete_agent": _delete_agent,
        "create_eval_script": _create_eval_script,
        "delete_eval_script": _delete_eval_script,
    }

    handler = handlers.get(tool_name)
    if not handler:
        log("ERROR", "CONFIRM_EXEC", f"未知写操作工具: {tool_name}",
            tool_args=tool_args)
        return {"ok": False, "error": f"未知的写操作: {tool_name}"}

    try:
        return await handler(tool_args, session)
    except Exception as exc:
        log("ERROR", "CONFIRM_EXEC", f"写操作 {tool_name} 执行异常",
            error=str(exc), error_type=type(exc).__name__, tool_args=tool_args)
        return {"ok": False, "error": f"执行失败: {exc}"}


# ═══════════════════════════════════════════════════════════════════════
# LLM 模型 CRUD
# ═══════════════════════════════════════════════════════════════════════

async def _create_llm_model(tool_args: dict, session: AsyncSession) -> dict:
    """创建 LLM 模型配置（api_key 留空，提示用户填写）。"""
    from database import LLMModel

    # 校验必填
    required = ["id", "name", "base_url", "model"]
    missing = [f for f in required if not tool_args.get(f)]
    if missing:
        return {"ok": False, "error": f"缺少必填字段: {', '.join(missing)}"}

    model_id = tool_args["id"]
    name = tool_args["name"]
    base_url = tool_args["base_url"]
    model_name = tool_args["model"]
    provider = tool_args.get("provider")
    is_default = bool(tool_args.get("is_default"))
    max_tokens = tool_args.get("max_tokens")
    temperature = tool_args.get("temperature")
    timeout = tool_args.get("timeout")

    # id 唯一性
    existing = await session.execute(
        select(LLMModel).where(LLMModel.id == model_id)
    )
    if existing.scalar_one_or_none():
        return {"ok": False, "error": f"模型标识 '{model_id}' 已存在"}

    # is_default 互斥
    if is_default:
        await session.execute(
            update(LLMModel).values(is_default=False)
        )

    model = LLMModel(
        id=model_id, name=name, provider=provider, base_url=base_url,
        model=model_name, api_key=None,
        is_default=is_default,
        max_tokens=max_tokens, temperature=temperature, timeout=timeout,
    )
    session.add(model)
    await session.commit()

    # 刷新缓存
    try:
        from main import refresh_llm_models_cache
        await refresh_llm_models_cache(session)
    except Exception as exc:
        log("WARN", "CONFIRM_EXEC", "刷新 LLM 模型缓存失败", error=str(exc))

    log("INFO", "CONFIRM_EXEC", "LLM 模型创建成功", model_id=model_id, name=name)
    return {
        "ok": True,
        "message": f"LLM 模型 '{model_id}' 创建成功，请到模型管理填写 API Key",
        "model": {
            "id": model_id, "name": name, "provider": provider,
            "base_url": base_url, "model": model_name,
            "is_default": is_default, "api_key": None,
        },
    }


async def _update_llm_model(tool_args: dict, session: AsyncSession) -> dict:
    """更新 LLM 模型配置（不含 api_key）。"""
    from database import LLMModel

    model_id = tool_args.get("id")
    if not model_id:
        return {"ok": False, "error": "缺少必填字段: id"}

    result = await session.execute(
        select(LLMModel).where(LLMModel.id == model_id)
    )
    model = result.scalar_one_or_none()
    if not model:
        return {"ok": False, "error": f"模型 '{model_id}' 不存在"}

    # is_default 互斥
    if tool_args.get("is_default") is True:
        await session.execute(
            update(LLMModel).where(LLMModel.id != model_id).values(is_default=False)
        )

    # 逐字段部分更新（不含 api_key）
    field_map = ["name", "provider", "base_url", "model", "is_default",
                 "max_tokens", "temperature", "timeout"]
    updated_fields = []
    for f in field_map:
        if tool_args.get(f) is not None:
            setattr(model, f, tool_args[f])
            updated_fields.append(f)

    model.updated_at = datetime.utcnow()
    await session.commit()

    try:
        from main import refresh_llm_models_cache
        await refresh_llm_models_cache(session)
    except Exception as exc:
        log("WARN", "CONFIRM_EXEC", "刷新 LLM 模型缓存失败", error=str(exc))

    log("INFO", "CONFIRM_EXEC", "LLM 模型更新成功",
        model_id=model_id, updated_fields=updated_fields)
    return {
        "ok": True,
        "message": f"LLM 模型 '{model_id}' 更新成功",
        "updated_fields": updated_fields,
    }


async def _delete_llm_model(tool_args: dict, session: AsyncSession) -> dict:
    """删除 LLM 模型配置。"""
    from database import LLMModel

    model_id = tool_args.get("id")
    if not model_id:
        return {"ok": False, "error": "缺少必填字段: id"}

    result = await session.execute(
        select(LLMModel).where(LLMModel.id == model_id)
    )
    model = result.scalar_one_or_none()
    if not model:
        return {"ok": False, "error": f"模型 '{model_id}' 不存在"}

    await session.delete(model)
    await session.commit()

    try:
        from main import refresh_llm_models_cache
        await refresh_llm_models_cache(session)
    except Exception as exc:
        log("WARN", "CONFIRM_EXEC", "刷新 LLM 模型缓存失败", error=str(exc))

    log("INFO", "CONFIRM_EXEC", "LLM 模型删除成功", model_id=model_id)
    return {"ok": True, "message": f"LLM 模型 '{model_id}' 删除成功"}


# ═══════════════════════════════════════════════════════════════════════
# Agent CRUD
# ═══════════════════════════════════════════════════════════════════════

def _assemble_inference_config(tool_args: dict) -> Optional[Any]:
    """从扁平 tool_args 组装 InferenceConfigSchema。任一推理字段存在则构造。"""
    infer_keys = ("url", "endpoint", "auth_type", "auth_token",
                  "workspace_id", "provider", "model")
    if not any(tool_args.get(k) for k in infer_keys):
        return None

    from schemas import InferenceConfigSchema

    auth_type = tool_args.get("auth_type") or "none"
    auth: dict[str, Any] = {"type": auth_type}
    if auth_type == "bearer":
        auth["token"] = tool_args.get("auth_token") or ""
    elif auth_type == "api_key":
        auth["api_key"] = tool_args.get("auth_token") or ""
    elif auth_type == "basic_auth":
        auth["username"] = tool_args.get("auth_username") or ""
        auth["password"] = tool_args.get("auth_password") or ""

    return InferenceConfigSchema(
        url=tool_args.get("url") or "",
        endpoint=tool_args.get("endpoint") or "/agentic/runs/sync",
        auth=auth,
        timeout=tool_args.get("timeout") or 1800,
        request_delay=tool_args.get("request_delay") or 0.1,
        workspace_id=tool_args.get("workspace_id"),
        workspace_name=tool_args.get("workspace_name"),
        provider=tool_args.get("provider"),
        model=tool_args.get("model"),
        model_version=tool_args.get("model_version"),
        model_id=tool_args.get("model_id"),
    )


async def _create_agent(tool_args: dict, session: AsyncSession) -> dict:
    """注册新 Agent。"""
    from database import Agent
    from main import (
        _derive_agent_kind,
        _ensure_agent_version_unique,
        _ensure_inference_config_probe,
        _resolve_langfuse_project_for_agent,
        agent_to_response,
    )
    from services.agent_identity import (
        ensure_agent_name_unique_for_logical,
        resolve_logical_agent_id_for_register,
    )
    from services.checkpoint_seed_service import seed_onto_agent_baseline_checkpoints

    name = tool_args.get("name")
    if not name or not str(name).strip():
        return {"ok": False, "error": "缺少必填字段: name"}
    name = str(name).strip()

    description = tool_args.get("description")
    version = tool_args.get("version")
    version_tag = tool_args.get("version_tag")
    logical_agent_id = tool_args.get("logical_agent_id")
    parent_agent_id = tool_args.get("parent_agent_id")
    skip_probe = bool(tool_args.get("skip_probe"))
    seed_default = tool_args.get("seed_default_checkpoints")
    if seed_default is None:
        seed_default = True

    inference_config = _assemble_inference_config(tool_args)

    # logical_agent_id 派生
    resolved_lid, parent_row = await resolve_logical_agent_id_for_register(
        session,
        logical_agent_id=logical_agent_id,
        parent_agent_id=parent_agent_id,
    )
    if parent_row:
        await session.flush()

    # 名称唯一性
    await ensure_agent_name_unique_for_logical(
        session, name, resolved_lid or None,
        parent_agent_id=parent_agent_id,
    )
    # 版本唯一性
    await _ensure_agent_version_unique(session, resolved_lid or None, version)
    # 健康预检（skip_probe=true 跳过，见风险点 5.1）
    await _ensure_inference_config_probe(inference_config, skip_probe)

    agent_id = f"agent_{uuid.uuid4().hex}"
    inference_config_str = json.dumps(inference_config.model_dump()) if inference_config else None

    langfuse_project = await _resolve_langfuse_project_for_agent(
        explicit=None,
        inference_config=inference_config,
        parent_langfuse=parent_row.langfuse_project if parent_row else None,
    )

    agent_kind = _derive_agent_kind(inference_config) if inference_config else "single"

    agent = Agent(
        id=agent_id, name=name, description=description,
        inference_config=inference_config_str,
        logical_agent_id=(resolved_lid or None),
        version=version, version_tag=version_tag,
        langfuse_project=langfuse_project,
        agent_kind=agent_kind,
    )
    session.add(agent)
    await session.flush()

    checkpoint_seed = await seed_onto_agent_baseline_checkpoints(
        session, agent, enabled=bool(seed_default),
    )
    await session.commit()

    log("INFO", "CONFIRM_EXEC", "Agent 注册成功",
        agent_id=agent_id, name=name, agent_kind=agent_kind)
    return {
        "ok": True,
        "message": f"Agent '{name}' 注册成功",
        "agent": agent_to_response(agent, checkpoint_seed=checkpoint_seed).model_dump(),
    }


async def _update_agent(tool_args: dict, session: AsyncSession) -> dict:
    """更新 Agent 配置（logical_agent_id/version 不可改）。"""
    from database import Agent
    from main import (
        _derive_agent_kind,
        _ensure_inference_config_probe,
        _resolve_langfuse_project_for_agent,
        agent_to_response,
    )
    from services.agent_identity import ensure_agent_name_unique_for_logical

    agent_id = tool_args.get("agent_id")
    if not agent_id:
        return {"ok": False, "error": "缺少必填字段: agent_id"}

    result = await session.execute(
        select(Agent).where(Agent.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        return {"ok": False, "error": f"Agent '{agent_id}' 不存在"}

    # 拒绝 logical_agent_id 变更
    if tool_args.get("logical_agent_id") is not None:
        new_lid = (tool_args["logical_agent_id"] or "").strip()
        old_lid = (agent.logical_agent_id or "").strip()
        if new_lid != old_lid:
            return {"ok": False, "error": "逻辑 Agent ID 创建后不可修改"}

    # 拒绝 version 变更
    if tool_args.get("version") is not None:
        new_ver = (tool_args["version"] or "").strip()
        old_ver = (agent.version or "").strip()
        if new_ver != old_ver:
            return {"ok": False, "error": "版本号创建后不可修改"}

    updated_fields = []

    # name 更新（同步到同 lid 的所有 agent）
    if tool_args.get("name") is not None:
        new_name = (tool_args["name"] or "").strip()
        if not new_name:
            return {"ok": False, "error": "Agent 名称不能为空"}
        old_name = (agent.name or "").strip()
        if new_name != old_name:
            await ensure_agent_name_unique_for_logical(
                session, new_name, agent.logical_agent_id, exclude_agent_id=agent_id,
            )
            lid = (agent.logical_agent_id or "").strip()
            if lid:
                siblings = await session.execute(
                    select(Agent).where(Agent.logical_agent_id == lid)
                )
                for row in siblings.scalars().all():
                    row.name = new_name
            else:
                agent.name = new_name
            updated_fields.append("name")

    if tool_args.get("description") is not None:
        agent.description = tool_args["description"]
        updated_fields.append("description")

    if tool_args.get("is_active") is not None:
        agent.is_active = bool(tool_args["is_active"])
        updated_fields.append("is_active")

    if tool_args.get("version_tag") is not None:
        agent.version_tag = tool_args["version_tag"]
        updated_fields.append("version_tag")

    # inference_config 更新（整体替换 + probe）
    skip_probe = bool(tool_args.get("skip_probe"))
    inference_config = _assemble_inference_config(tool_args)
    if inference_config is not None:
        await _ensure_inference_config_probe(inference_config, skip_probe)
        agent.inference_config = json.dumps(inference_config.model_dump())
        agent.agent_kind = _derive_agent_kind(inference_config)
        updated_fields.append("inference_config")

    # langfuse 同步
    should_refresh_langfuse = (
        tool_args.get("langfuse_project") is not None or inference_config is not None
    )
    if should_refresh_langfuse:
        infer_cfg: Optional[Any] = inference_config
        if infer_cfg is None and agent.inference_config:
            infer_cfg = json.loads(agent.inference_config)
        langfuse_project = await _resolve_langfuse_project_for_agent(
            explicit=tool_args.get("langfuse_project"),
            inference_config=infer_cfg,
            parent_langfuse=None,
        )
        lid = (agent.logical_agent_id or "").strip()
        if lid:
            siblings = await session.execute(
                select(Agent).where(Agent.logical_agent_id == lid)
            )
            for row in siblings.scalars().all():
                row.langfuse_project = langfuse_project
        else:
            agent.langfuse_project = langfuse_project
        updated_fields.append("langfuse_project")

    await session.commit()

    log("INFO", "CONFIRM_EXEC", "Agent 更新成功",
        agent_id=agent_id, updated_fields=updated_fields)
    return {
        "ok": True,
        "message": f"Agent '{agent_id}' 更新成功",
        "updated_fields": updated_fields,
        "agent": agent_to_response(agent).model_dump(),
    }


async def _delete_agent(tool_args: dict, session: AsyncSession) -> dict:
    """删除 Agent（物理删除，无依赖检查）。"""
    from database import Agent

    agent_id = tool_args.get("agent_id")
    if not agent_id:
        return {"ok": False, "error": "缺少必填字段: agent_id"}

    result = await session.execute(
        select(Agent).where(Agent.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        return {"ok": False, "error": f"Agent '{agent_id}' 不存在"}

    await session.delete(agent)
    await session.commit()

    log("INFO", "CONFIRM_EXEC", "Agent 删除成功", agent_id=agent_id)
    return {"ok": True, "message": f"Agent '{agent_id}' 删除成功"}


# ═══════════════════════════════════════════════════════════════════════
# 评估脚本 CRUD
# ═══════════════════════════════════════════════════════════════════════

async def _create_eval_script(tool_args: dict, session: AsyncSession) -> dict:
    """创建评估脚本（validate_script 校验）。"""
    from database import EvaluationScript
    from evaluators.custom_script_metric import validate_script

    name = tool_args.get("name")
    script_code = tool_args.get("script_code")
    if not name:
        return {"ok": False, "error": "缺少必填字段: name"}
    if not script_code:
        return {"ok": False, "error": "缺少必填字段: script_code"}

    # 校验代码
    validation = validate_script(script_code)
    if not validation.get("valid"):
        return {"ok": False, "error": f"脚本验证失败: {validation.get('error', '未知错误')}"}

    description = tool_args.get("description")
    script_id = f"script_{uuid.uuid4().hex}"

    script = EvaluationScript(
        id=script_id, name=name, description=description, script_code=script_code,
    )
    session.add(script)
    await session.commit()

    log("INFO", "CONFIRM_EXEC", "评估脚本创建成功",
        script_id=script_id, name=name)
    return {
        "ok": True,
        "message": f"评估脚本 '{name}' 创建成功",
        "script": {"id": script_id, "name": name, "description": description},
    }


async def _delete_eval_script(tool_args: dict, session: AsyncSession) -> dict:
    """删除评估脚本。"""
    from database import EvaluationScript

    script_id = tool_args.get("script_id")
    if not script_id:
        return {"ok": False, "error": "缺少必填字段: script_id"}

    result = await session.execute(
        select(EvaluationScript).where(EvaluationScript.id == script_id)
    )
    script = result.scalar_one_or_none()
    if not script:
        return {"ok": False, "error": f"脚本 '{script_id}' 不存在"}

    await session.delete(script)
    await session.commit()

    log("INFO", "CONFIRM_EXEC", "评估脚本删除成功", script_id=script_id)
    return {"ok": True, "message": f"评估脚本 '{script_id}' 删除成功"}


