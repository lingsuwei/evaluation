"""onto-eval AI 对话式配置 — 配置类工具集（阶段 2）。

7 个工具，每个含两部分：
- OpenAI function schema（给 LLM 看的声明，见 ALL_TOOL_SCHEMAS）
- async 执行函数（实际调用，返回精简结构给 LLM）

工具分两类：
- 查询类（list_*/get_*/recommend_*）：同进程直接查 DB，返回精简结构
- 动作类（build_eval_request）：组装草稿并落库 ChatSession.draft_request

执行函数签名统一：async def fn(session: AsyncSession, **args) -> dict
orchestrator 传入共享 session（同一会话内复用，避免每次工具调用都新建连接）。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Dataset, Agent, LLMModel, CustomMetric, EvaluationScript, EvaluationTask, EvaluationResult
from logger import log


# ═══════════════════════════════════════════════════════════════════════
# 工具 Schema（OpenAI function 格式）
# ═══════════════════════════════════════════════════════════════════════

LIST_DATASETS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_datasets",
        "description": "列出系统中所有可用的评测数据集，返回每个数据集的 ID、名称和题目数量。当用户提到数据集名称但不确定 ID 时调用。",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

LIST_AGENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_agents",
        "description": "列出所有已激活的 Agent，返回 ID、名称、逻辑AgentID、版本。在线评估需要 agent_id 时调用。",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

LIST_LLM_MODELS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_llm_models",
        "description": "列出已配置的评测 LLM 模型，返回模型 key、显示名称、是否默认。用于确定 llm_model 字段。",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

LIST_METRICS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_metrics",
        "description": "列出所有可用的评测指标，返回指标 key、中文名、类别。用于确定 selected_metrics 字段。可按评估模式过滤。",
        "parameters": {
            "type": "object",
            "properties": {
                "evaluation_mode": {
                    "type": "string",
                    "enum": ["online", "offline"],
                    "description": "评估模式，过滤该模式不支持的指标",
                },
            },
            "required": [],
        },
    },
}

GET_DATASET_PREVIEW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_dataset_preview",
        "description": "预览指定数据集的字段分布和场景键，确认数据集内容匹配评测需求。",
        "parameters": {
            "type": "object",
            "properties": {
                "dataset_id": {
                    "type": "string",
                    "description": "数据集 ID",
                },
            },
            "required": ["dataset_id"],
        },
    },
}

RECOMMEND_METRICS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "recommend_metrics",
        "description": "根据评测意图推荐合适的指标组合。当用户不确定选哪些指标时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "online_mode": {
                    "type": "boolean",
                    "description": "是否在线评估",
                },
                "focus": {
                    "type": "string",
                    "description": "评测关注点，如'忠实度''正确性''相关性'，可逗号分隔多个",
                },
            },
            "required": ["online_mode"],
        },
    },
}

BUILD_EVAL_REQUEST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "build_eval_request",
        "description": (
            "组装评测任务配置草稿。每收集到一个字段就可以调用，会增量合并到草稿中。"
            "所有字段确认后即可完成配置。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "dataset_id": {"type": "string", "description": "数据集 ID"},
                "online_mode": {"type": "boolean", "description": "是否在线评估"},
                "agent_id": {"type": "string", "description": "Agent ID（在线模式必填）"},
                "llm_model": {
                    "type": "string",
                    "description": (
                        "评测 LLM 模型 key。用户明确指定模型名时填对应 key；"
                        "用户说'默认模型/默认评估模型/不指定'时留空（不要填 'glm' 或 'auto' 等占位字符串），"
                        "后端会自动取模型管理里 is_default 标记的模型。"
                    ),
                },
                "inference_rounds": {
                    "type": "integer",
                    "description": "推理轮次 1-10",
                    "minimum": 1,
                    "maximum": 10,
                },
                "selected_metrics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "评估因子 key 列表",
                },
                "use_bm25": {"type": "boolean", "description": "是否启用 BM25 忠实度"},
                "use_doc_library": {"type": "boolean", "description": "是否使用文档库"},
                "ontology_package": {"type": "string", "description": "本体包名称"},
                "auto_scenario_score": {"type": "boolean", "description": "是否自动场景打分"},
                "enable_process_consistency": {"type": "boolean", "description": "是否评测过程一致性"},
                "enable_stability": {"type": "boolean", "description": "是否评测运行稳定性（仅在线有意义）"},
                "parallel_inference": {"type": "boolean", "description": "在线推理并发开关，True=并发，False=题目+轮次全串行（小参数模型 Agent 建议 False）"},
                "selected_indices": {"type": "array", "items": {"type": "integer"}, "description": "评测项索引列表（0-based），None 或空数组表示全部题目"},
                "custom_metric_ids": {"type": "array", "items": {"type": "integer"}, "description": "自定义评测指标 ID 列表（来自 list_metrics 返回的 custom 类指标的 id）"},
                "custom_scripts": {"type": "object", "description": "用例索引→脚本ID 映射，如 {\"0\": \"script_xxx\", \"2\": \"script_yyy\"}，指定用例用脚本评估代替 LLM"},
                "llm_max_tokens": {"type": "integer", "description": "临时覆盖评测LLM最大输出tokens（512-8192）", "minimum": 512, "maximum": 8192},
                "llm_temperature": {"type": "number", "description": "临时覆盖评测LLM温度（0.0-1.0）", "minimum": 0, "maximum": 1},
                "langfuse_source": {
                    "type": "object",
                    "description": "离线评估时从 Langfuse 获取历史答案的配置，仅 online_mode=false 时有效",
                    "properties": {
                        "project": {"type": "string", "description": "Langfuse 项目 ID"},
                        "source_task_id": {"type": "string", "description": "来源评测任务 ID，拉取其下所有 trace 的答案"},
                        "trace_ids": {"type": "array", "items": {"type": "string"}, "description": "指定 trace ID 列表"},
                        "tags": {"type": "string", "description": "按标签筛选 trace，逗号分隔"},
                        "session_id": {"type": "string", "description": "按 session ID 筛选 trace"},
                        "agent_name": {"type": "string", "description": "按 Agent 名称筛选 trace"},
                        "enable_process_eval": {"type": "boolean", "description": "启用过程回放，默认 true"},
                    },
                },
            },
            "required": [],
        },
    },
}

LIST_EVAL_SCRIPTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_eval_scripts",
        "description": "列出所有评估脚本，返回 id/name/description。当用户指定某题用脚本评估时调用，获取可用脚本 ID 用于 custom_scripts 字段。",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

LIST_ONTOLOGY_PACKAGES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_ontology_packages",
        "description": "列出所有本体包（BM25 忠实度评估的参考文档目录），返回名称和 md 文件数量。启用 use_bm25 或 use_doc_library 时调用确认名称存在。",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

# ═══════════════════════════════════════════════════════════════════════
# 写操作工具 Schema（requires_confirm，由 orchestrator 拦截 → 确认后执行）
# ═══════════════════════════════════════════════════════════════════════

CREATE_LLM_MODEL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_llm_model",
        "description": "创建 LLM 模型配置。不含 API Key，创建后请提示用户到模型管理页填写。",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "模型标识 key，如 glm"},
                "name": {"type": "string", "description": "显示名称，如 智谱GLM-4.7"},
                "base_url": {"type": "string", "description": "API 基础 URL"},
                "model": {"type": "string", "description": "模型名称，如 glm-4.7"},
                "provider": {"type": "string", "description": "模型厂商"},
                "is_default": {"type": "boolean", "description": "是否设为默认模型"},
                "max_tokens": {"type": "integer", "description": "最大输出 tokens"},
                "temperature": {"type": "number", "description": "温度 0.0-1.0"},
                "timeout": {"type": "number", "description": "API 超时秒数"},
            },
            "required": ["id", "name", "base_url", "model"],
        },
    },
}

UPDATE_LLM_MODEL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_llm_model",
        "description": "更新已有的 LLM 模型配置。仅传入需要修改的字段，不能修改 api_key。",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "要更新的模型标识 key"},
                "name": {"type": "string", "description": "新的显示名称"},
                "base_url": {"type": "string", "description": "新的 API 基础 URL"},
                "model": {"type": "string", "description": "新的模型名称"},
                "provider": {"type": "string", "description": "新的模型厂商"},
                "is_default": {"type": "boolean", "description": "是否设为默认模型"},
                "max_tokens": {"type": "integer", "description": "最大输出 tokens"},
                "temperature": {"type": "number", "description": "温度参数"},
                "timeout": {"type": "number", "description": "API 超时秒数"},
            },
            "required": ["id"],
        },
    },
}

DELETE_LLM_MODEL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delete_llm_model",
        "description": "删除 LLM 模型配置。不可逆操作，请确认后再执行。",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "要删除的模型标识 key"},
            },
            "required": ["id"],
        },
    },
}

CREATE_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_agent",
        "description": "注册新 Agent。建议设 skip_probe=true 避免健康预检超时（测试环境尤其需要）。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Agent 名称"},
                "description": {"type": "string", "description": "Agent 描述"},
                "url": {"type": "string", "description": "推理服务地址"},
                "endpoint": {"type": "string", "description": "接口端点，默认 /agentic/runs/sync"},
                "auth_type": {"type": "string", "enum": ["none", "bearer", "api_key", "basic_auth"], "description": "认证方式"},
                "auth_token": {"type": "string", "description": "bearer/api_key 的 token"},
                "auth_username": {"type": "string", "description": "basic_auth 用户名"},
                "auth_password": {"type": "string", "description": "basic_auth 密码"},
                "timeout": {"type": "integer", "description": "推理超时秒数，默认 1800"},
                "request_delay": {"type": "number", "description": "请求间隔秒数"},
                "workspace_id": {"type": "string", "description": "工作空间 ID"},
                "workspace_name": {"type": "string", "description": "工作空间展示名"},
                "provider": {"type": "string", "description": "被测推理模型厂商"},
                "model": {"type": "string", "description": "被测推理模型名称"},
                "model_version": {"type": "string", "description": "模型版本"},
                "model_id": {"type": "string", "description": "模型 ID 或部署 ID"},
                "version": {"type": "string", "description": "版本号"},
                "version_tag": {"type": "string", "description": "版本标签"},
                "logical_agent_id": {"type": "string", "description": "逻辑 Agent ID（多版本共享）"},
                "parent_agent_id": {"type": "string", "description": "父 Agent ID（注册新版本时传入）"},
                "skip_probe": {"type": "boolean", "description": "跳过健康预检，建议 true"},
                "seed_default_checkpoints": {"type": "boolean", "description": "是否预置默认过程点，默认 true"},
            },
            "required": ["name"],
        },
    },
}

UPDATE_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_agent",
        "description": "更新已有的 Agent 配置。仅传入需要修改的字段。注意：logical_agent_id 和 version 创建后不可修改。",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "要更新的 Agent ID"},
                "name": {"type": "string", "description": "新的 Agent 名称"},
                "description": {"type": "string", "description": "新的描述"},
                "is_active": {"type": "boolean", "description": "是否激活"},
                "url": {"type": "string", "description": "推理服务地址"},
                "endpoint": {"type": "string", "description": "接口端点"},
                "auth_type": {"type": "string", "enum": ["none", "bearer", "api_key", "basic_auth"], "description": "认证方式"},
                "auth_token": {"type": "string", "description": "bearer/api_key 的 token"},
                "auth_username": {"type": "string", "description": "basic_auth 用户名"},
                "auth_password": {"type": "string", "description": "basic_auth 密码"},
                "timeout": {"type": "integer", "description": "推理超时秒数"},
                "request_delay": {"type": "number", "description": "请求间隔秒数"},
                "workspace_id": {"type": "string", "description": "工作空间 ID"},
                "workspace_name": {"type": "string", "description": "工作空间展示名"},
                "provider": {"type": "string", "description": "被测推理模型厂商"},
                "model": {"type": "string", "description": "被测推理模型名称"},
                "model_version": {"type": "string", "description": "模型版本"},
                "model_id": {"type": "string", "description": "模型 ID"},
                "version_tag": {"type": "string", "description": "版本标签"},
                "langfuse_project": {"type": "string", "description": "Langfuse 项目 ID"},
                "skip_probe": {"type": "boolean", "description": "跳过健康预检"},
            },
            "required": ["agent_id"],
        },
    },
}

DELETE_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delete_agent",
        "description": "删除 Agent。不可逆操作，请确认后再执行。",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "要删除的 Agent ID"},
            },
            "required": ["agent_id"],
        },
    },
}

CREATE_EVAL_SCRIPT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_eval_script",
        "description": "创建评估脚本。script_code 必须定义 evaluate(question, answer, context=None, ground_truth=None) -> dict，返回含 score 字段。创建前自动校验代码。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "脚本名称"},
                "description": {"type": "string", "description": "脚本描述"},
                "script_code": {"type": "string", "description": "Python 代码，定义 evaluate 函数"},
            },
            "required": ["name", "script_code"],
        },
    },
}

DELETE_EVAL_SCRIPT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delete_eval_script",
        "description": "删除评估脚本。不可逆操作，请确认后再执行。",
        "parameters": {
            "type": "object",
            "properties": {
                "script_id": {"type": "string", "description": "要删除的脚本 ID"},
            },
            "required": ["script_id"],
        },
    },
}

ALL_TOOL_SCHEMAS = [
    LIST_DATASETS_SCHEMA,
    LIST_AGENTS_SCHEMA,
    LIST_LLM_MODELS_SCHEMA,
    LIST_METRICS_SCHEMA,
    LIST_EVAL_SCRIPTS_SCHEMA,
    LIST_ONTOLOGY_PACKAGES_SCHEMA,
    GET_DATASET_PREVIEW_SCHEMA,
    RECOMMEND_METRICS_SCHEMA,
    BUILD_EVAL_REQUEST_SCHEMA,
    CREATE_LLM_MODEL_SCHEMA,
    UPDATE_LLM_MODEL_SCHEMA,
    DELETE_LLM_MODEL_SCHEMA,
    CREATE_AGENT_SCHEMA,
    UPDATE_AGENT_SCHEMA,
    DELETE_AGENT_SCHEMA,
    CREATE_EVAL_SCRIPT_SCHEMA,
    DELETE_EVAL_SCRIPT_SCHEMA,
]

# 草稿必填字段（用于 build_eval_request 计算 missing_fields）
_REQUIRED_FIELDS = ["dataset_id"]

# ═══════════════════════════════════════════════════════════════════════
# 工具安全级别
# ═══════════════════════════════════════════════════════════════════════

SAFETY_READ_ONLY = "read_only"            # 查询类，自动执行无副作用
SAFETY_DRAFT_ONLY = "draft_only"          # build_eval_request，写草稿不写库
SAFETY_AUTO_EXECUTE = "auto_execute"      # 启动类（launch_eval），自主执行不弹确认卡
SAFETY_REQUIRES_CONFIRM = "requires_confirm"  # 写操作，需用户确认后执行


# ═══════════════════════════════════════════════════════════════════════
# launch_eval：基于会话草稿直接启动评估（无需 LLM 重复产出字段）
# ═══════════════════════════════════════════════════════════════════════

LAUNCH_EVAL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "launch_eval",
        "description": (
            "基于当前会话已组装的评测草稿（build_eval_request 产出的 draft_request）"
            "直接启动评估任务。无需重复传入字段，草稿字段已存于会话。"
            "调用后系统自动创建评测任务并启动后台执行，返回 task_uuid。"
            "前置条件：草稿已含 dataset_id（在线模式还需 agent_id）。"
            "适用场景：用户在对话中已确认配置无误，要求'启动'/'开始评估'/'跑起来'时调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


# ═══════════════════════════════════════════════════════════════════════
# 工具执行函数
# ═══════════════════════════════════════════════════════════════════════


async def list_datasets(session: AsyncSession, **kwargs) -> dict:
    """列出所有数据集（精简字段）。"""
    result = await session.execute(select(Dataset).order_by(Dataset.created_at.desc()))
    rows = result.scalars().all()
    return {
        "items": [
            {"id": r.id, "name": r.name, "item_count": r.item_count or 0}
            for r in rows
        ],
        "total": len(rows),
    }


async def list_agents(session: AsyncSession, **kwargs) -> dict:
    """列出已激活 Agent（精简字段）。"""
    result = await session.execute(
        select(Agent).where(Agent.is_active == True)  # noqa: E712
    )
    rows = result.scalars().all()
    return {
        "items": [
            {
                "id": r.id,
                "name": r.name,
                "logical_agent_id": r.logical_agent_id,
                "version": r.version,
                "agent_kind": r.agent_kind,
            }
            for r in rows
        ],
        "total": len(rows),
    }


async def list_llm_models(session: AsyncSession, **kwargs) -> dict:
    """列出已配置 LLM 模型（精简字段，剔除 api_key）。"""
    result = await session.execute(select(LLMModel))
    rows = result.scalars().all()
    return {
        "items": [
            {
                "key": r.id,
                "name": r.name,
                "is_default": bool(r.is_default),
                "provider": r.provider,
            }
            for r in rows
        ],
        "total": len(rows),
    }


async def list_metrics(
    session: AsyncSession,
    evaluation_mode: Optional[str] = None,
    **kwargs,
) -> dict:
    """列出可用评测指标（扁平化 builtin + deepeval + custom）。

    复用 main.py:list_metrics 的分组逻辑，但扁平化为 [{key, label, category}] 给 LLM。
    """
    from config import METRICS_CONFIG

    offline_only = (evaluation_mode or "").strip().lower() == "offline"
    _RUNTIME_KEYS = ("inference_latency", "tokens_total")

    # 内置指标
    _BUILTIN_LABELS = {
        "faithfulness": "忠实度",
        "answer_relevancy": "答案相关性",
        "correctness": "正确性",
        "inference_latency": "推理延迟",
        "tokens_total": "Token总数",
    }
    items: list[dict[str, Any]] = []
    for k in METRICS_CONFIG.keys():
        if offline_only and k in _RUNTIME_KEYS:
            continue
        items.append({
            "key": k,
            "label": _BUILTIN_LABELS.get(k, k),
            "category": "builtin",
        })

    # DeepEval 指标
    try:
        from evaluators.deepeval_metrics.deepeval_suite import (
            METRIC_SPECS, is_metric_offline_supported,
        )
        for spec in METRIC_SPECS:
            if spec.key in ("faithfulness", "answer_relevancy", "golden_match", "hallucination"):
                continue
            if offline_only and not is_metric_offline_supported(spec):
                continue
            items.append({
                "key": spec.key,
                "label": spec.label,
                "category": "deepeval",
            })
    except ImportError:
        pass

    # 自定义指标
    result = await session.execute(
        select(CustomMetric).where(CustomMetric.is_active == True)  # noqa: E712
    )
    for cm in result.scalars():
        if offline_only and cm.multi_turn:
            continue
        items.append({
            "key": f"custom_{cm.id}",
            "id": cm.id,
            "label": cm.name,
            "category": "custom",
        })

    return {"items": items, "total": len(items)}


async def list_eval_scripts(session: AsyncSession, **kwargs) -> dict:
    """列出所有评估脚本（精简字段，不含代码内容）。"""
    result = await session.execute(
        select(EvaluationScript).order_by(EvaluationScript.created_at.desc())
    )
    rows = result.scalars().all()
    return {
        "items": [
            {
                "id": r.id,
                "name": r.name,
                "description": r.description,
            }
            for r in rows
        ],
        "total": len(rows),
    }


async def list_ontology_packages(session: AsyncSession, **kwargs) -> dict:
    """列出所有本体包（BM25 忠实度评估的参考文档目录）。

    复用 main.py 的 _get_ontology_base_dir 路径解析逻辑，
    遍历 backend/md/ 子目录统计 .md 文件数。
    """
    import os
    try:
        from main import _get_ontology_base_dir
    except ImportError:
        # 兜底：直接按相对路径定位
        from config import BASE_DIR
        base_dir = os.path.join(BASE_DIR, "md")
    else:
        base_dir = _get_ontology_base_dir()

    packages: list[dict[str, Any]] = []
    if os.path.exists(base_dir):
        for name in os.listdir(base_dir):
            pkg_dir = os.path.join(base_dir, name)
            if not os.path.isdir(pkg_dir):
                continue
            md_count = sum(
                1 for root, _, files in os.walk(pkg_dir)
                for f in files if f.endswith(".md")
            )
            packages.append({"name": name, "md_count": md_count})

    return {"items": packages, "total": len(packages)}


async def get_dataset_preview(
    session: AsyncSession,
    dataset_id: str,
    **kwargs,
) -> dict:
    """预览数据集字段分布与场景键。"""
    result = await session.execute(
        select(Dataset).where(Dataset.id == dataset_id)
    )
    ds = result.scalar_one_or_none()
    if not ds:
        return {"error": f"数据集 {dataset_id} 不存在", "found": False}

    try:
        data = json.loads(ds.data) if ds.data else []
    except (ValueError, TypeError):
        data = []

    # 统计字段分布（前 20 题采样）
    sample = data[:20] if isinstance(data, list) else []
    field_set: set[str] = set()
    scenario_keys: set[str] = set()
    for item in sample:
        if isinstance(item, dict):
            field_set.update(item.keys())
            sk = item.get("scenario_key") or item.get("scenario")
            if sk:
                scenario_keys.add(str(sk))

    return {
        "id": ds.id,
        "name": ds.name,
        "item_count": ds.item_count or 0,
        "fields": sorted(field_set),
        "scenario_keys": sorted(scenario_keys),
        "found": True,
    }


async def recommend_metrics(
    session: AsyncSession,
    online_mode: bool,
    focus: Optional[str] = None,
    **kwargs,
) -> dict:
    """按评测意图推荐指标组合。

    规则：
    - focus 含"忠实"→ faithfulness
    - focus 含"相关/切题"→ answer_relevancy
    - focus 含"正确/准确"→ correctness
    - 无 focus → 三个质量指标全选
    - online_mode → 追加 inference_latency + tokens_total
    """
    recommended: list[str] = []
    reasons: list[str] = []
    f = (focus or "").lower()

    has_focus = bool(f.strip())
    if not has_focus:
        recommended = ["faithfulness", "answer_relevancy", "correctness"]
        reasons.append("用户未指定关注点，默认三质量指标全选")
    else:
        if "忠实" in f or "faithful" in f:
            recommended.append("faithfulness")
            reasons.append("关注忠实度")
        if "相关" in f or "切题" in f or "relevan" in f:
            recommended.append("answer_relevancy")
            reasons.append("关注答案相关性")
        if "正确" in f or "准确" in f or "correct" in f:
            recommended.append("correctness")
            reasons.append("关注正确性")
        # 未匹配任何关键词 → 兜底全选
        if not recommended:
            recommended = ["faithfulness", "answer_relevancy", "correctness"]
            reasons.append("未匹配到具体关注点，默认三质量指标全选")

    if online_mode:
        recommended.extend(["inference_latency", "tokens_total"])
        reasons.append("在线评估追加运行稳定性指标")

    return {
        "recommended": recommended,
        "reason": "；".join(reasons),
    }


async def build_eval_request(
    session: AsyncSession,
    session_id: str,
    **fields,
) -> dict:
    """组装评测配置草稿，增量合并到 ChatSession.draft_request。

    Args:
        session: DB 会话
        session_id: 聊天会话 ID（orchestrator 注入）
        **fields: EvaluateRequest 字段（dataset_id/online_mode/agent_id/...）

    Returns:
        {updated_fields, draft, missing_fields}
    """
    from chat.session import update_draft

    # 过滤 None 值（LLM 可能传 null 表示"不更新"）
    patch = {k: v for k, v in fields.items() if v is not None}
    if not patch:
        return {
            "updated_fields": [],
            "draft": None,
            "missing_fields": _REQUIRED_FIELDS,
            "message": "无字段更新（所有字段为 null）",
        }

    # 约束 inference_rounds 到 1-10
    if "inference_rounds" in patch:
        try:
            r = int(patch["inference_rounds"])
            patch["inference_rounds"] = max(1, min(10, r))
        except (TypeError, ValueError):
            patch.pop("inference_rounds", None)

    # llm_model 兜底：LLM 填的 key 在 DB 缓存里查不到 / 对应模型缺 api_key 时，
    # 清空字段让下游 create_and_start_eval_task 走"取默认模型"路径
    # （用户说"默认评估模型"时 LLM 仍可能误填 "glm" 等占位字符串，这里服务端兜底）
    fallback_to_default = False
    if "llm_model" in patch:
        llm_key = str(patch["llm_model"]).strip()
        if llm_key and llm_key.lower() != "auto":
            from config import _db_llm_models_cache
            cfg = _db_llm_models_cache.get(llm_key)
            if not cfg or not (cfg.get("api_key") or "").strip():
                fallback_to_default = True
                patch.pop("llm_model", None)
                log("WARNING", "CHAT_TOOLS", "llm_model 不存在或无 API Key，回落默认",
                    session_id=session_id, llm_model=llm_key,
                    has_cfg=bool(cfg), has_api_key=bool(cfg and cfg.get("api_key")))

    updated = await update_draft(session_id, patch, session)
    if not updated:
        return {
            "updated_fields": [],
            "draft": None,
            "missing_fields": _REQUIRED_FIELDS,
            "error": f"会话 {session_id} 不存在",
        }

    try:
        draft = json.loads(updated.draft_request) if updated.draft_request else {}
    except (ValueError, TypeError):
        draft = {}

    # 计算缺失必填字段
    missing: list[str] = []
    for f in _REQUIRED_FIELDS:
        if not draft.get(f):
            missing.append(f)
    # 在线模式必须有 agent_id
    if draft.get("online_mode") and not draft.get("agent_id"):
        missing.append("agent_id")

    log("INFO", "CHAT_TOOLS", "build_eval_request 草稿更新",
        session_id=session_id, updated_fields=list(patch.keys()),
        missing=missing, fallback_to_default=fallback_to_default)

    return {
        "updated_fields": list(patch.keys()),
        "draft": draft,
        "missing_fields": missing,
        **({"fallback_to_default": True,
            "message": "llm_model 已回落到默认模型（用户指定的模型未配置或无 API Key）"}
           if fallback_to_default else {}),
    }


# ═══════════════════════════════════════════════════════════════════════
# launch_eval：基于会话草稿直接启动评估（自主执行，无需确认卡）
# ═══════════════════════════════════════════════════════════════════════

async def launch_eval_handler(session: AsyncSession, session_id: str, **kwargs) -> dict:
    """基于会话草稿直接启动评估任务（自主执行，不弹确认卡）。

    从 confirm_executor._launch_eval 迁移而来，保留全部草稿校验 +
    create_and_start_eval_task 调用 + 后台任务调度 + mark_consumed 标记。
    execute_tool 会注入 session_id（与 build_eval_request 同机制）。
    """
    import asyncio
    import json as _json

    from fastapi import BackgroundTasks, HTTPException

    from chat import session as chat_session_svc
    from schemas import EvaluateRequest
    from services.eval_task_service import create_and_start_eval_task

    if not session_id:
        return {"ok": False, "error": "缺少 session_id，无法读取会话草稿"}

    obj = await chat_session_svc.get_session(session_id, session)
    if not obj:
        return {"ok": False, "error": f"会话 {session_id} 不存在"}

    if obj.status not in ("ready", "consumed"):
        return {"ok": False, "error": f"会话状态为 {obj.status}，需先组装草稿后再启动"}

    if not obj.draft_request:
        return {"ok": False, "error": "草稿为空，无法启动评测"}

    try:
        draft_dict = _json.loads(obj.draft_request)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": f"草稿 JSON 解析失败: {exc}"}

    if not draft_dict.get("dataset_id"):
        return {"ok": False, "error": "草稿缺少 dataset_id，无法启动评测"}

    online_mode = draft_dict.get("online_mode", False)
    if online_mode and not draft_dict.get("agent_id"):
        return {"ok": False, "error": "在线评估需指定 agent_id"}

    # llm_model 兜底：草稿里残留的 key 在 DB 缓存里查不到 / 无 api_key 时，
    # 清空字段让 create_and_start_eval_task 走"取默认模型"路径
    llm_key_in_draft = str(draft_dict.get("llm_model") or "").strip()
    if llm_key_in_draft and llm_key_in_draft.lower() != "auto":
        from config import _db_llm_models_cache
        cfg = _db_llm_models_cache.get(llm_key_in_draft)
        if not cfg or not (cfg.get("api_key") or "").strip():
            log("WARNING", "CHAT_TOOLS", "launch_eval 草稿 llm_model 失效，回落默认",
                session_id=session_id, llm_model=llm_key_in_draft)
            draft_dict.pop("llm_model", None)

    try:
        eval_req = EvaluateRequest(**draft_dict)
    except Exception as exc:
        return {"ok": False, "error": f"草稿字段非法: {exc}"}

    # 手动实例化 BackgroundTasks（不在请求上下文里）
    bg = BackgroundTasks()
    try:
        resp = await create_and_start_eval_task(eval_req, session, bg)
    except HTTPException as exc:
        return {"ok": False, "error": exc.detail}
    except Exception as exc:
        return {"ok": False, "error": f"启动评测失败: {exc}"}

    # 调度 bg.tasks：FastAPI 在请求结束时会自动跑，这里手动启动
    for bg_task in bg.tasks:
        try:
            asyncio.create_task(bg_task.func(*bg_task.args, **bg_task.kwargs))
        except Exception as exc:
            log("ERROR", "CHAT_TOOLS", "调度后台评估任务失败",
                task_uuid=resp.task_uuid, error=str(exc))
            # 任务行已创建，不阻断——后台任务可由其他机制重试或人工查看

    await chat_session_svc.mark_consumed(session_id, resp.task_uuid, session)

    log("INFO", "CHAT_TOOLS", "评估任务已通过 AI 助手自主启动",
        session_id=session_id, task_uuid=resp.task_uuid,
        online_mode=online_mode, dataset_id=draft_dict.get("dataset_id"))

    return {
        "ok": True,
        "message": f"评估任务已启动（task={resp.task_uuid}）",
        "task_uuid": resp.task_uuid,
    }


# ═══════════════════════════════════════════════════════════════════════
# 任务查询与报告工具（read_only，支持闭环：读结果+出报告）
# ═══════════════════════════════════════════════════════════════════════

GET_TASK_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_task_status",
        "description": (
            "查询评估任务的状态和进度（精简，不算质量分）。"
            "适用场景：用户问'任务 task_xxx 完成了吗/跑到哪了'时调用。"
            "返回 {task_id, status, progress_current, progress_total, error, created_at, completed_at}。"
            "status 取值：pending/processing/completed/failed/paused/cancelled。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 UUID"},
            },
            "required": ["task_id"],
        },
    },
}

GET_EVAL_DETAIL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_eval_detail",
        "description": (
            "获取任务的完整评测详情摘要：质量总分 + 四维结论（quality/process_consistency/"
            "scenario_delivery/runtime_stability 每维 status+headline）+ 题目数 + 失败题目前 5 条。"
            "用于生成评测简报。返回结构化 dict，请用 Markdown 表格输出。"
            "深挖逐题 judge 明细请用 get_round_results。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 UUID"},
            },
            "required": ["task_id"],
        },
    },
}

GET_ROUND_RESULTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_round_results",
        "description": (
            "按题+轮次分组返回 judge 明细（每轮 score + reason 摘要）。"
            "适用场景：用户问'哪几题失败了/某题为什么扣分/详细 judge 理由'时调用。"
            "默认只返回未通过题目，最多 10 题；如需全部指定 limit。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 UUID"},
                "question_id": {"type": "string", "description": "可选，指定题目 ID 只查该题"},
                "only_failed": {"type": "boolean", "description": "只返回未通过题目，默认 true"},
                "limit": {"type": "integer", "description": "最多返回题目数，默认 10"},
            },
            "required": ["task_id"],
        },
    },
}

EXPORT_REPORT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "export_report",
        "description": (
            "触发后端生成 xlsx 评测报告（复用 /tasks/export 能力）。"
            "返回 {ok, file_token}，前端会自动下载。"
            "适用场景：用户要'导出报告/下载 Excel'时调用。"
            "LLM 不读 xlsx 内容，只触发并告知用户下载链接已生成。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "任务 UUID 列表（1-6 个）",
                },
            },
            "required": ["task_ids"],
        },
    },
}

COMPARE_TASKS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "compare_tasks",
        "description": (
            "对比多个已完成任务（2-6 个），返回每个任务的总分+四维徽章+相对 baseline 的差值。"
            "适用场景：用户要'对比 task_a 和 task_b/哪个更好/差异在哪'时调用。"
            "返回结构化 dict，请用 Markdown 表格输出对比矩阵。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "任务 UUID 列表（2-6 个）",
                },
                "baseline_task_id": {"type": "string", "description": "可选，基线任务 ID，默认第一个"},
            },
            "required": ["task_ids"],
        },
    },
}


async def get_task_status(session: AsyncSession, task_id: str, **kwargs) -> dict:
    """查询任务状态和进度（精简字段）。"""
    task = await session.get(EvaluationTask, task_id)
    if not task:
        return {"found": False, "error": f"任务 {task_id} 不存在"}
    return {
        "found": True,
        "task_id": task.id,
        "status": task.status,
        "progress_current": task.progress_current,
        "progress_total": task.progress_total,
        "error": (task.error[:500] if task.error else None),
        "evaluation_mode": task.evaluation_mode,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


async def get_eval_detail(session: AsyncSession, task_id: str, **kwargs) -> dict:
    """获取任务完整评测详情摘要（精简，防止 token 膨胀）。"""
    from services.export_report_service import collect_task_data
    from services.task_dimension_verdict_service import (
        build_task_dimension_verdicts,
        compact_dimension_verdict_badges,
    )

    task = await session.get(EvaluationTask, task_id)
    if not task:
        return {"found": False, "error": f"任务 {task_id} 不存在"}

    if (task.status or "").lower() not in ("completed", "cancelled", "failed"):
        return {
            "found": True,
            "task_id": task_id,
            "status": task.status,
            "message": "任务尚未完成，暂无评测详情。请用 get_task_status 轮询。",
        }

    # 复用 collect_task_data 取完整聚合（含 quality_score + dimension_verdicts + questions）
    data = await collect_task_data(session, task_id)
    if not data:
        return {"found": True, "task_id": task_id, "status": task.status, "error": "无聚合数据"}

    qs = data.get("quality_score") or {}
    dv = data.get("dimension_verdicts") or {}
    badges = compact_dimension_verdict_badges(dv) if dv else {}

    # 失败题目 top5
    questions = data.get("questions") or []
    failed = [
        {
            "question_id": q.get("question_id"),
            "metric_name": q.get("metric_name"),
            "score": q.get("score"),
            "passed": q.get("passed"),
        }
        for q in questions
        if q.get("passed") is False
    ][:5]

    # 四维 headline 摘要
    dim_summary = {}
    for key in ("quality", "process_consistency", "scenario_delivery", "runtime_stability"):
        v = dv.get(key) or {}
        dim_summary[key] = {
            "status": v.get("status"),
            "headline": v.get("headline"),
            "score": v.get("score"),
        }

    return {
        "found": True,
        "task_id": task_id,
        "status": task.status,
        "evaluation_mode": task.evaluation_mode,
        "total_score": qs.get("total_score"),
        "result_score": qs.get("result_score"),
        "process_score": qs.get("process_score"),
        "dimension_badges": badges,
        "dimension_summary": dim_summary,
        "question_count": len(questions),
        "failed_question_count": sum(1 for q in questions if q.get("passed") is False),
        "failed_questions_top5": failed,
    }


async def get_round_results(
    session: AsyncSession,
    task_id: str,
    question_id: Optional[str] = None,
    only_failed: bool = True,
    limit: int = 10,
    **kwargs,
) -> dict:
    """按题+轮次分组返回 judge 明细。"""
    from services.export_report_service import _load_rounds_by_result

    task = await session.get(EvaluationTask, task_id)
    if not task:
        return {"found": False, "error": f"任务 {task_id} 不存在"}

    rows = (
        await session.execute(
            select(EvaluationResult)
            .where(EvaluationResult.task_id == task_id)
            .order_by(EvaluationResult.question_id)
        )
    ).scalars().all()

    if question_id:
        rows = [r for r in rows if r.question_id == question_id]
    if only_failed:
        rows = [r for r in rows if r.passed is False]
    rows = rows[:limit]

    if not rows:
        return {"found": True, "task_id": task_id, "items": [], "message": "无匹配题目"}

    # 批量加载轮次
    result_ids = [r.id for r in rows]
    rounds_by_result = await _load_rounds_by_result(session, result_ids)

    items = []
    for r in rows:
        rounds = rounds_by_result.get(r.id, [])
        items.append({
            "question_id": r.question_id,
            "metric_name": r.metric_name,
            "score": r.score,
            "passed": r.passed,
            "threshold": r.threshold,
            "agent_answer": (r.agent_answer or "")[:300] if r.agent_answer else None,
            "eval_reason": (r.eval_reason or "")[:500] if r.eval_reason else None,
            "rounds": [
                {
                    "round_num": rd.get("round_num"),
                    "score": rd.get("score"),
                    "reason": (rd.get("reason") or "")[:400],
                    "duration_ms": rd.get("duration_ms"),
                }
                for rd in rounds
            ],
        })

    return {
        "found": True,
        "task_id": task_id,
        "filter": {"question_id": question_id, "only_failed": only_failed, "limit": limit},
        "items": items,
    }


async def export_report(session: AsyncSession, task_ids: list, **kwargs) -> dict:
    """触发生成 xlsx 评测报告，返回下载 file_token。

    复用 export_report_service.build_export_report 生成 bytes，
    写入临时文件并返回 token（前端通过现有 /tasks/export 端点二次下载，
    或直接由前端监听本工具结果自行触发下载）。

    本工具不返回 bytes 给 LLM（避免上下文爆炸），只返回 token。
    """
    import os
    import tempfile
    import uuid as _uuid

    from services.export_report_service import build_export_report

    if not task_ids or len(task_ids) < 1:
        return {"ok": False, "error": "task_ids 不能为空"}
    if len(task_ids) > 6:
        return {"ok": False, "error": "单次最多导出 6 个任务"}

    try:
        xlsx_bytes = await build_export_report(session, list(task_ids))
    except Exception as exc:
        log("ERROR", "CHAT_TOOLS", "export_report 生成失败",
            task_ids=task_ids, error=str(exc))
        return {"ok": False, "error": f"生成报告失败: {exc}"}

    if not xlsx_bytes:
        return {"ok": False, "error": "生成报告为空，任务可能未完成"}

    # 写临时文件，前端通过 file_token 拉取（沿用 main.py 的临时文件机制）
    file_token = f"chat_export_{_uuid.uuid4().hex[:12]}"
    tmp_dir = tempfile.gettempdir()
    file_path = os.path.join(tmp_dir, f"{file_token}.xlsx")
    with open(file_path, "wb") as f:
        f.write(xlsx_bytes)

    log("INFO", "CHAT_TOOLS", "export_report 已生成",
        task_ids=task_ids, file_token=file_token, size=len(xlsx_bytes))

    return {
        "ok": True,
        "file_token": file_token,
        "file_name": f"eval_report_{len(task_ids)}tasks.xlsx",
        "size_bytes": len(xlsx_bytes),
        "message": f"报告已生成（{len(task_ids)} 个任务），前端将自动下载",
    }


async def compare_tasks(
    session: AsyncSession,
    task_ids: list,
    baseline_task_id: Optional[str] = None,
    **kwargs,
) -> dict:
    """对比多个任务，返回精简摘要。"""
    from services.task_compare_service import compare_tasks_summary

    return await compare_tasks_summary(session, list(task_ids), baseline_task_id)


# ═══════════════════════════════════════════════════════════════════════
# 工具注册表：name → (schema, execution function, safety_level)
# ═══════════════════════════════════════════════════════════════════════

TOOL_REGISTRY: dict[str, tuple[dict, Any, str]] = {
    "list_datasets": (LIST_DATASETS_SCHEMA, list_datasets, SAFETY_READ_ONLY),
    "list_agents": (LIST_AGENTS_SCHEMA, list_agents, SAFETY_READ_ONLY),
    "list_llm_models": (LIST_LLM_MODELS_SCHEMA, list_llm_models, SAFETY_READ_ONLY),
    "list_metrics": (LIST_METRICS_SCHEMA, list_metrics, SAFETY_READ_ONLY),
    "list_eval_scripts": (LIST_EVAL_SCRIPTS_SCHEMA, list_eval_scripts, SAFETY_READ_ONLY),
    "list_ontology_packages": (LIST_ONTOLOGY_PACKAGES_SCHEMA, list_ontology_packages, SAFETY_READ_ONLY),
    "get_dataset_preview": (GET_DATASET_PREVIEW_SCHEMA, get_dataset_preview, SAFETY_READ_ONLY),
    "recommend_metrics": (RECOMMEND_METRICS_SCHEMA, recommend_metrics, SAFETY_READ_ONLY),
    "get_task_status": (GET_TASK_STATUS_SCHEMA, get_task_status, SAFETY_READ_ONLY),
    "get_eval_detail": (GET_EVAL_DETAIL_SCHEMA, get_eval_detail, SAFETY_READ_ONLY),
    "get_round_results": (GET_ROUND_RESULTS_SCHEMA, get_round_results, SAFETY_READ_ONLY),
    "export_report": (EXPORT_REPORT_SCHEMA, export_report, SAFETY_READ_ONLY),
    "compare_tasks": (COMPARE_TASKS_SCHEMA, compare_tasks, SAFETY_READ_ONLY),
    "build_eval_request": (BUILD_EVAL_REQUEST_SCHEMA, build_eval_request, SAFETY_DRAFT_ONLY),
    # 写操作工具：执行函数为 None（由 orchestrator 拦截 → confirm 端点执行）
    "create_llm_model": (CREATE_LLM_MODEL_SCHEMA, None, SAFETY_REQUIRES_CONFIRM),
    "update_llm_model": (UPDATE_LLM_MODEL_SCHEMA, None, SAFETY_REQUIRES_CONFIRM),
    "delete_llm_model": (DELETE_LLM_MODEL_SCHEMA, None, SAFETY_REQUIRES_CONFIRM),
    "create_agent": (CREATE_AGENT_SCHEMA, None, SAFETY_REQUIRES_CONFIRM),
    "update_agent": (UPDATE_AGENT_SCHEMA, None, SAFETY_REQUIRES_CONFIRM),
    "delete_agent": (DELETE_AGENT_SCHEMA, None, SAFETY_REQUIRES_CONFIRM),
    "create_eval_script": (CREATE_EVAL_SCRIPT_SCHEMA, None, SAFETY_REQUIRES_CONFIRM),
    "delete_eval_script": (DELETE_EVAL_SCRIPT_SCHEMA, None, SAFETY_REQUIRES_CONFIRM),
    "launch_eval": (LAUNCH_EVAL_SCHEMA, launch_eval_handler, SAFETY_AUTO_EXECUTE),
}


def get_tool_safety(tool_name: str) -> str:
    """返回工具的安全级别，未注册工具默认 read_only。"""
    entry = TOOL_REGISTRY.get(tool_name)
    return entry[2] if entry else SAFETY_READ_ONLY


def get_tool_schemas() -> list[dict]:
    """返回所有工具 schema（给 LLM 的 tools 参数，不含安全级别）。"""
    return [entry[0] for entry in TOOL_REGISTRY.values()]


async def execute_tool(
    tool_name: str,
    session: AsyncSession,
    session_id: str,
    tool_args: dict,
) -> dict:
    """执行指定工具，返回结果 dict。

    仅执行 read_only / draft_only 工具。requires_confirm 工具由
    orchestrator 特殊处理（暂停等待用户确认），不走此函数。

    Args:
        tool_name: 工具名
        session: DB 会话
        session_id: 聊天会话 ID（仅 build_eval_request 需要）
        tool_args: LLM 传入的参数（已 JSON 解析）
    """
    entry = TOOL_REGISTRY.get(tool_name)
    if not entry:
        return {"error": f"未知工具: {tool_name}"}
    _schema, fn, safety = entry

    # requires_confirm 工具不应走到这里（orchestrator 拦截）
    if safety == SAFETY_REQUIRES_CONFIRM:
        return {"error": f"写操作工具 {tool_name} 需经确认端点执行，不应直接调用 execute_tool"}

    # build_eval_request 和 launch_eval 需要额外注入 session_id
    kwargs = dict(tool_args)
    if tool_name in ("build_eval_request", "launch_eval"):
        kwargs["session_id"] = session_id

    try:
        result = await fn(session, **kwargs)
        return result
    except Exception as exc:
        log("ERROR", "CHAT_TOOLS", f"工具 {tool_name} 执行失败",
            error=str(exc), tool_args=tool_args)
        return {"error": f"工具执行失败: {exc}"}
