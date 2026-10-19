方案：场景步骤 LLM 汇聚 + 关键词匹配评测
一、整体流程
code
┌──────────────────────────────────────────────────────┐
│           场景子步骤打分流程（compute_run_scenario_steps）       │
├──────────────────────────────────────────────────────┤
│                                                      │
│  1. Span 绑定 → 提取 step output (score_text)         │
│                    ↓                                  │
│  2. 读取 match_rules.eval_config.mode                 │
│                    ↓                                  │
│  ┌── "keyword" 或空 ──→ 现有 _score_step (关键词匹配)   │
│  │                                                    │
│  └── "llm_aggregate" ──→ 新流程:                      │
│         │                                             │
│         ├─ a. LLM 二次汇聚                             │
│         │   aggregate_prompt + score_text → LLM       │
│         │   → aggregated_answer                       │
│         │                                             │
│         ├─ b. 关键词匹配                               │
│         │   aggregated_answer vs gt_expected.keywords  │
│         │   → score / passed                          │
│         │                                             │
│         └─ c. evidence 记录                           │
│             含 aggregate_prompt / aggregated_answer    │
│                                                      │
└──────────────────────────────────────────────────────┘
二、数据结构
2.1 match_rules 扩展（无需 DB migration）
match_rules 已是 JSON 字段，在其中新增 eval_config：

json
{
  "trace_binding": {
    "span_name_patterns": ["llm:main_iteration", "llm:main"],
    "output_keywords": ["答案", "结论"],
    "label": "生成答案"
  },
  "eval_config": {
    "mode": "llm_aggregate",
    "aggregate_prompt": "请根据以下Agent推理步骤的输出，提取并汇总所有作战单元的变更信息，按格式输出：变更编号、变更类型、变更前MPart、变更后MPart。",
    "llm_model": null
  }
}
字段	类型	说明
eval_config.mode	"keyword" | "llm_aggregate" | null	评测模式，空或 "keyword" 走现有关键词逻辑
eval_config.aggregate_prompt	string	LLM 二次汇聚的系统级提示词，指导 LLM 如何从步骤 output 中抽取结构化信息
eval_config.llm_model	string | null	指定汇聚使用的 LLM 模型 key，null 则使用任务默认模型
三、后端改动清单
3.1 新增 services/scenario_llm_aggregate_service.py
python
"""场景步骤 LLM 二次汇聚服务。"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from logger import log


async def llm_aggregate_step_output(
    step_output: str,
    aggregate_prompt: str,
    llm_model: Optional[str] = None,
) -> str:
    """
    对步骤 output 执行 LLM 二次汇聚，返回汇聚后的文本。
    
    Args:
        step_output: 从 Span 绑定提取的步骤 output 文本
        aggregate_prompt: 汇聚提示词（来自 match_rules.eval_config.aggregate_prompt）
        llm_model: 可选的 LLM 模型 key
    
    Returns:
        汇聚后的文本
    """
    from evaluators.llm_client import evaluate_with_llm

    if not step_output or not step_output.strip():
        return ""

    prompt = f"""{aggregate_prompt}

【Agent 推理输出】
{step_output}

请严格按照上述要求对 Agent 推理输出进行汇聚，直接输出汇聚结果，不要添加多余的解释。"""

    result = await evaluate_with_llm(
        prompt,
        system_prompt="你是一个专业的信息抽取与汇聚助手。请严格按照用户要求对内容进行结构化汇总。",
        model_key=llm_model,
    )

    log(
        "INFO",
        "SCENARIO_LLM_AGG",
        "LLM 二次汇聚完成",
        prompt_preview=aggregate_prompt[:80],
        input_length=len(step_output),
        output_length=len(result),
    )

    return result or ""


def parse_eval_config(match_rules: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从 match_rules 中解析 eval_config，返回带默认值的结构。"""
    if not match_rules or not isinstance(match_rules, dict):
        return {"mode": "keyword", "aggregate_prompt": "", "llm_model": None}
    config = match_rules.get("eval_config") or {}
    if not isinstance(config, dict):
        return {"mode": "keyword", "aggregate_prompt": "", "llm_model": None}
    return {
        "mode": (config.get("mode") or "keyword"),
        "aggregate_prompt": (config.get("aggregate_prompt") or "").strip(),
        "llm_model": config.get("llm_model"),
    }
3.2 修改 services/scenario_step_trace_service.py
核心改动在 compute_run_scenario_steps 中，在 _resolve_step_score_text 之后、_score_step 之前插入 LLM 汇聚分支：

python
# --- 在 compute_run_scenario_steps 的 for step in active_steps 循环中 ---

# 现有逻辑
score_text, score_source = _resolve_step_score_text(
    step_key, io, bind_meta, agent_answer, final_answer_steps=fa_steps
)

# ★ 新增：LLM 汇聚分支
eval_config = parse_eval_config(step.get("match_rules"))
aggregate_prompt = ""
aggregated_answer = ""

if eval_config["mode"] == "llm_aggregate" and eval_config["aggregate_prompt"]:
    if score_text:  # 步骤有绑定的 output 才做汇聚
        aggregated_answer = await llm_aggregate_step_output(
            step_output=score_text,
            aggregate_prompt=eval_config["aggregate_prompt"],
            llm_model=eval_config.get("llm_model"),
        )
        score_text = aggregated_answer  # 替换为汇聚结果
        score_source = "llm_aggregate"
    else:
        score_source = "llm_aggregate_no_input"

# 现有逻辑（不变）
score, passed, span_ev = _score_step(score_text, step.get("gt_expected"))
evidence 中补充 LLM 汇聚信息：

python
evidence = {
    **bind_meta,
    **span_ev,
    "score_source": score_source,
    "observation_source": obs_source,
}
# ★ 新增
if eval_config["mode"] == "llm_aggregate":
    evidence["eval_mode"] = "llm_aggregate"
    evidence["aggregate_prompt_preview"] = eval_config["aggregate_prompt"][:200]
    evidence["aggregated_answer_preview"] = aggregated_answer[:500] if aggregated_answer else ""
3.3 修改 services/scenario_eval_service.py
_score_step 无需改动 — 汇聚后的文本已替换 score_text，关键词匹配逻辑复用现有实现即可。

但需将 _score_step 对 LLM 汇聚结果的匹配情况写入 evidence，方便区分"原始 output 匹配"和"汇聚后匹配"。

3.4 修改 services/scenario_step_verify_service.py
preview_scenario_step_on_trace 和 batch_preview_scenario_steps_for_item 需要支持 LLM 汇聚模式的试跑：

试跑时如果步骤配置了 eval_config.mode = "llm_aggregate"，则：
绑定 Span → 提取 output
调用 LLM 汇聚 → 返回 aggregated_answer_preview
用汇聚结果做关键词匹配预判
在返回结果中新增字段：
json
{
  "eval_mode": "llm_aggregate",
  "aggregate_prompt_preview": "...",
  "aggregated_answer_preview": "...",
  "aggregate_keyword_hits": ["A001", "B001"]
}
3.5 修改 services/dataset_item_scenario_service.py
ensure_step_match_rules 中补全 eval_config 默认值（保持向后兼容）：

python
def ensure_step_match_rules(
    *,
    match_rules: Optional[Dict[str, Any]],
    gt_expected: Optional[Dict[str, Any]] = None,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    base = dict(match_rules) if isinstance(match_rules, dict) else {}
    # ... 现有 trace_binding 补全逻辑 ...
    
    # ★ 新增：eval_config 默认值
    if "eval_config" not in base:
        base["eval_config"] = {"mode": "keyword"}
    
    return base
3.6 修改 scenario_routes.py
ScenarioItemStepCreateRequest 和 ScenarioItemStepUpdateRequest 已有 match_rules: Optional[dict] 字段，前端传入包含 eval_config 的 JSON 即可，无需修改 schema。

试跑 API ScenarioStepPreviewTraceRequest 同理，match_rules 字段已支持。

四、前端改动清单
4.1 ScenarioItemStepsPanel.tsx
在步骤编辑表单中新增「汇聚提示词」区域：

code
┌─────────────────────────────────────────────────────────┐
│  编辑子步骤                                              │
│                                                         │
│  step_key: [S3]     名称: [生成答案]                      │
│  顺序: [3]          权重: [0.40]                          │
│                                                         │
│  ▸ 评测模式: (●) 关键词匹配  (○) LLM 二次汇聚             │
│                                                         │
│  ┌─ LLM 二次汇聚配置 ─────────────────────────────┐     │
│  │ 汇聚提示词:                                      │     │
│  │ ┌──────────────────────────────────────────┐    │     │
│  │ │ 请根据以下Agent推理步骤的输出，提取并汇总    │    │     │
│  │ │ 所有作战单元的变更信息，按格式输出：         │    │     │
│  │ │ 变更编号、变更类型、变更前MPart、变更后MPart │    │     │
│  │ └──────────────────────────────────────────┘    │     │
│  │                                                 │     │
│  │ LLM 模型 (可选): [默认 ▾]                       │     │
│  └─────────────────────────────────────────────────┘     │
│                                                         │
│  命中关键词（逗号分隔）: [A001, B001, 450元, 变更]          │
│                                                         │
└─────────────────────────────────────────────────────────┘
4.2 ScenarioStepTraceVerifyPanel.tsx
试跑结果中新增 LLM 汇聚信息的展示：

code
步骤3：生成答案
  绑定: llm:main_iteration (iteration=3) ✓
  评测模式: LLM 二次汇聚
  汇聚结果预览: "A001: MPart M001→M002; B001: MPart B001→B002; 450元"
  关键词命中: A001 ✓  B001 ✓  450元 ✓  变更 ✓
五、完整文件改动列表
文件	改动类型	说明
backend/services/scenario_llm_aggregate_service.py	新增	LLM 汇聚服务 + eval_config 解析
backend/services/scenario_step_trace_service.py	修改	compute_run_scenario_steps 增加 LLM 汇聚分支
backend/services/scenario_step_verify_service.py	修改	试跑 API 支持 LLM 汇聚模式
backend/services/dataset_item_scenario_service.py	修改	ensure_step_match_rules 补全 eval_config 默认值
backend/scenario_routes.py	无修改	已有 match_rules 字段透传
frontend/src/components/ScenarioItemStepsPanel.tsx	修改	表单增加评测模式切换 + 汇聚提示词编辑
frontend/src/components/ScenarioStepTraceVerifyPanel.tsx	修改	试跑结果展示 LLM 汇聚信息
六、使用示例
对于任务 task_065efea7ec1d468891ac37aa74635420，评测集「东风测试0612new」中某题的步骤3配置：

json
{
  "step_key": "S3",
  "name": "生成答案",
  "step_order": 3,
  "weight": 0.40,
  "gt_expected": {
    "keywords": ["A001", "B001", "450元", "变更"]
  },
  "match_rules": {
    "trace_binding": {
      "span_name_patterns": ["llm:main_iteration", "llm:main"],
      "output_keywords": ["答案", "结论"],
      "label": "生成答案"
    },
    "eval_config": {
      "mode": "llm_aggregate",
      "aggregate_prompt": "请根据以下Agent推理步骤的输出，提取并汇总所有作战单元的变更信息，按格式输出：变更编号、变更类型、变更前MPart、变更后MPart。"
    }
  }
}
执行流程：

Span 绑定 → 提取步骤3的 output（Agent 原始推理文本）
LLM 二次汇聚 → 输出结构化汇总（如 "A001: MPart M001→M002; B001: MPart B001→B002; 总成本 450元"）
关键词匹配 → A001 ✓ B001 ✓ 450元 ✓ 变更 ✓ → score=1.0
七、向后兼容
eval_config.mode 默认为 "keyword"，未配置时走现有逻辑
match_rules 中没有 eval_config 时，parse_eval_config 返回 mode="keyword"
现有步骤的 match_rules 无需任何迁移