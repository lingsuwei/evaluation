"""onto-eval AI 对话式配置 — 意图解析引擎（阶段 1）。

单轮 LLM 调用：自然语言 → EvaluateRequest 草稿。
不含 tool-calling 循环（阶段 2 由 orchestrator.py 扩展）。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from chat.llm_router import chat_completion, parse_json_loose
from logger import log

# ── System Prompt ──
_SYSTEM_PROMPT = """你是 onto-eval 本体评测平台的配置助手。用户会用自然语言描述评测需求，你需要把它解析为结构化的评测任务配置（EvaluateRequest）。

输出必须是 JSON，字段如下：
{
  "dataset_id": "数据集ID，用户没提则null",
  "online_mode": true或false,
  "agent_id": "Agent ID，离线评估或用户没提则null",
  "llm_model": "模型key，如glm；用户没提则null",
  "inference_rounds": 1,
  "selected_metrics": ["faithfulness","answer_relevancy","correctness"],
  "use_bm25": false,
  "use_doc_library": false,
  "ontology_package": null,
  "auto_scenario_score": true,
  "enable_process_consistency": true,
  "enable_stability": true,
  "parallel_inference": true,
  "selected_indices": null,
  "custom_metric_ids": null,
  "custom_scripts": null,
  "llm_max_tokens": null,
  "llm_temperature": null,
  "langfuse_source": null,
  "confidence": 0.85,
  "missing_fields": [],
  "suggestions": []
}

可用指标说明：
- faithfulness: 忠实度，回答是否基于上下文，不编造
- answer_relevancy: 答案相关性，是否切题
- correctness: 正确性，与标准答案对比

规则：
1. 在线评估(online_mode=true)必须有agent_id；离线评估不需要
2. 离线评估(online_mode=false)时 enable_stability 通常无意义(无实时推理)，建议设false
3. 在线评估建议开启 enable_stability 评测运行稳定性
4. 用户说"跑N轮"/"跑几次"映射 inference_rounds(1-10)，未提默认1
5. 用户没明确说在线/离线，默认在线(online_mode=true)
6. 用户没提到的布尔字段，按上面JSON里的默认值填
7. selected_metrics：用户提到"忠实度"加faithfulness，"相关性/切题"加answer_relevancy，"正确性/准确"加correctness；没提则用三个全选
8. use_bm25 仅对 faithfulness 有效，用户明确提到BM25才设true
9. confidence：你对这次解析的把握，0-1
10. missing_fields：必填但用户没提供的字段名（dataset_id 在线模式还有 agent_id），已识别的不要列
11. suggestions：给用户的1-3条建议（如"在线评估建议开启稳定性维度"），无可留空数组
12. 只输出JSON，不要额外解释，不要markdown围栏
13. parallel_inference：在线推理并发开关，默认true(并发)。用户提到"串行"/"小参数模型"/"避免并发干扰"时设false
14. selected_indices：评测项索引列表(0-based)。用户说"前N题"填[0,1,...,N-1]；"第X题"填[X-1]；"跳过第X题"排除该索引；未提则null表示全部
15. custom_metric_ids：用户提到自定义指标时填ID列表(整数)，如用户说"用自定义指标1和3"填[1,3]；未提则null
16. custom_scripts：用户指定某题用脚本评估时填映射，如"第0题用脚本"填{"0":"脚本ID"}；脚本ID需用户明确提供或从后续工具查询；未提则null
17. llm_max_tokens：用户提到"最大输出/最大token"时填(512-8192)；未提则null
18. llm_temperature：用户提到"温度"时填(0.0-1.0)；未提则null
19. langfuse_source：离线评估时从Langfuse获取历史答案的配置。用户提到"从Langfuse拉答案/用历史trace/用langfuse项目xxx的答案"时填充对象：{project:"项目ID", source_task_id:"来源任务ID", tags:"标签", session_id:"会话ID", agent_name:"Agent名", enable_process_eval:true}。仅离线评估有效，在线评估留null"""


def _empty_draft() -> dict[str, Any]:
    """返回 EvaluateRequest 的最小默认草稿（用户意图无法解析时的兜底）。"""
    return {
        "dataset_id": None,
        "online_mode": True,
        "agent_id": None,
        "llm_model": None,
        "inference_rounds": 1,
        "selected_metrics": ["faithfulness", "answer_relevancy", "correctness"],
        "use_bm25": False,
        "use_doc_library": False,
        "ontology_package": None,
        "auto_scenario_score": True,
        "enable_process_consistency": True,
        "enable_stability": True,
        "parallel_inference": True,
        "selected_indices": None,
        "custom_metric_ids": None,
        "custom_scripts": None,
        "llm_max_tokens": None,
        "llm_temperature": None,
        "langfuse_source": None,
    }


def _normalize_draft(raw: dict[str, Any]) -> dict[str, Any]:
    """把 LLM 输出归一化为 EvaluateRequest 兼容字段（剔除 confidence/missing_fields/suggestions）。"""
    keep = [
        "dataset_id", "online_mode", "agent_id", "llm_model",
        "inference_rounds", "selected_metrics", "use_bm25", "use_doc_library",
        "ontology_package", "auto_scenario_score", "enable_process_consistency",
        "enable_stability",
        "parallel_inference", "selected_indices", "custom_metric_ids",
        "custom_scripts", "llm_max_tokens", "llm_temperature", "langfuse_source",
    ]
    out = _empty_draft()
    for k in keep:
        if k in raw and raw[k] is not None:
            out[k] = raw[k]
    # 约束 inference_rounds 到 1-10
    try:
        r = int(out.get("inference_rounds", 1))
        out["inference_rounds"] = max(1, min(10, r))
    except (TypeError, ValueError):
        out["inference_rounds"] = 1
    # 在线模式必须有 agent_id —— 缺失则记入 missing
    return out


async def interpret_intent(
    intent_text: str,
    model_key: Optional[str] = None,
) -> dict[str, Any]:
    """单轮意图解析：自然语言 → EvaluateRequest 草稿。

    Returns:
        {
            "draft": {...},          # EvaluateRequest 字段
            "confidence": 0.0-1.0,
            "missing_fields": [...],
            "suggestions": [...],
            "intent": "online" | "offline" | "unknown",
        }
    """
    if not intent_text or not intent_text.strip():
        draft = _empty_draft()
        return {
            "draft": draft,
            "confidence": 0.0,
            "missing_fields": ["dataset_id", "agent_id"],
            "suggestions": ["请描述你的评测意图，例如：用 GLM 评估 agent-001 在数据集 ds_faq 上的忠实度，跑3轮"],
            "intent": "unknown",
        }

    content, tokens = await chat_completion(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=intent_text,
        model_key=model_key,
    )

    parsed = parse_json_loose(content)
    if not parsed:
        log("WARNING", "CHAT_RECOMMEND", "LLM 输出非JSON，降级兜底",
            content_preview=content[:200] if content else "空")
        draft = _empty_draft()
        return {
            "draft": draft,
            "confidence": 0.0,
            "missing_fields": ["dataset_id", "agent_id"],
            "suggestions": ["未能解析意图，请补充：数据集、Agent、评估指标、轮次等信息"],
            "intent": "unknown",
        }

    draft = _normalize_draft(parsed)
    confidence = float(parsed.get("confidence", 0.0) or 0.0)
    missing_fields = parsed.get("missing_fields") or []
    if not isinstance(missing_fields, list):
        missing_fields = []
    suggestions = parsed.get("suggestions") or []
    if not isinstance(suggestions, list):
        suggestions = []

    # 补充校验：在线但无 agent_id
    if draft.get("online_mode") and not draft.get("agent_id"):
        if "agent_id" not in missing_fields:
            missing_fields.append("agent_id")
    # 必填 dataset_id
    if not draft.get("dataset_id") and "dataset_id" not in missing_fields:
        missing_fields.append("dataset_id")

    intent = "online" if draft.get("online_mode") else ("offline" if draft.get("online_mode") is False else "unknown")

    log("INFO", "CHAT_RECOMMEND", "意图解析完成",
        intent=intent, confidence=confidence,
        missing=missing_fields, dataset_id=draft.get("dataset_id"),
        agent_id=draft.get("agent_id"), tokens=tokens)

    return {
        "draft": draft,
        "confidence": confidence,
        "missing_fields": missing_fields,
        "suggestions": suggestions,
        "intent": intent,
    }
