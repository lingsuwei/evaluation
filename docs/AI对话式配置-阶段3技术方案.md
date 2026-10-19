# AI 对话式配置 — 阶段 3 技术方案（智能生成）

> **版本**：v1.0
> **日期**：2026-08-14
> **状态**：待实施
> **上级方案**：[AI对话式配置-整体架构方案.md](./AI对话式配置-整体架构方案.md)
> **前置阶段**：[阶段1技术方案](./AI对话式配置-阶段1技术方案.md)（已实施）、[阶段2技术方案](./AI对话式配置-阶段2技术方案.md)（待实施）
> **阶段定位**：追加 4 个生成类工具，覆盖最专业的配置项（评测集/场景模板/judge prompt/阈值），把"懂行才能配"的门槛降下来

---

## 1. 范围与边界

### 1.1 做什么

在阶段 2 的 agent loop + 7 个配置类工具基础上，**纯追加 4 个生成类工具**。LLM 通过 tool-calling 在对话中生成专业配置产物：

| 工具 | 生成什么 | 替代的手工操作 |
|---|---|---|
| `generate_dataset_from_description` | 评测集（question/ground_truth/context） | 手工编写 Excel/JSON 上传 |
| `generate_scenario_template` | 场景子步骤模板（step/weight/gt_criteria） | `ScenarioItemStepsPanel` 手工配子步骤 |
| `tune_judge_prompt` | 优化 G-Eval 的 criteria 评判标准 | `CustomMetricManager` 手工写自然语言标准 |
| `recommend_verdict_thresholds` | 四维结论阈值推荐 | `VerdictThresholdEditor` 手工调 13 个阈值 |

### 1.2 不做什么

- ❌ 改动阶段 1/2 的任何已有模块（纯追加）
- ❌ 生成 DAG/code 类自定义指标（仅优化 geval 的 criteria，DAG/code 结构复杂不宜 LLM 生成）
- ❌ 自动应用生成的阈值/场景模板（生成产物后仍需人工确认/微调）
- ❌ 批量生成（单次工具调用生成单份数据集/模板，不一次性产出 300 题）

### 1.3 与阶段 2 的关系

阶段 3 **不新增任何架构组件**。4 个工具写进 `chat/tools.py`，schema 注册到 `orchestrator.py` 的工具清单，agent loop 自动可调用。前端 `ToolCallBubble` 复用阶段 2 的渲染，无需新组件。

```
阶段 2 产出                    阶段 3 纯追加
─────────────                ──────────────────
orchestrator.py agent loop    (不变,新工具自动被调)
chat/tools.py 7个工具         +4 个生成类工具
llm_router tool-calling       (不变)
SSE 事件 + ToolCallBubble     (不变)
ChatMessage tool_* 字段       (不变)
```

---

## 2. 数据流

4 个工具的生成模式统一：

```
用户对话描述需求
      │
      ▼
LLM 决定调用某生成类工具（tool_call）
      │
      ▼
orchestrator 执行工具函数:
┌─────────────────────────────────────────────┐
│  1. (部分工具)读取现有数据作为上下文          │
│     - tune_judge_prompt: 读现有 criteria     │
│     - recommend_verdict_thresholds: 读历史结果│
│  2. 调 LLM 生成产物（chat_completion 单轮）   │
│  3. 解析 LLM 输出（parse_json_loose 容错）    │
│  4. (部分工具)落库生成的产物                  │
│     - generate_dataset: 写 Dataset 表         │
│     - generate_scenario: 写 ScenarioEval* 表  │
│     - tune_judge_prompt: 更新 CustomMetric   │
│  5. 返回产物摘要给 agent loop（tool_result） │
└─────────────────────────────────────────────┘
      │
      ▼
emit chat_tool_result（含产物预览）
      │
      ▼
LLM 收到 tool_result，用自然语言告诉用户:
  "已生成 N 题评测集 ds_xxx，请到评测管理查看/微调"
      │
      ▼
agent loop 结束 / 继续下一轮
```

> **关键**：生成类工具内部**自己调 LLM**（用 `chat_completion`），与 agent loop 外层的 LLM 调用是**嵌套关系**。外层 LLM 决定"何时生成"，内层 LLM 执行"生成什么"。

---

## 3. 工具设计

### 3.1 `generate_dataset_from_description` — 生成评测集

#### Schema

```json
{
  "type": "function",
  "function": {
    "name": "generate_dataset_from_description",
    "description": "根据自然语言需求描述生成评测数据集。LLM 生成 question/ground_truth/context 等字段并落库为新数据集。适用于快速构造评测集原型。",
    "parameters": {
      "type": "object",
      "properties": {
        "description": {
          "type": "string",
          "description": "评测需求描述，如'生成10道关于本体知识问答的评测题，考察Agent的事实准确性'"
        },
        "name": {
          "type": "string",
          "description": "数据集名称，如'本体知识问答v1'"
        },
        "question_count": {
          "type": "integer",
          "description": "生成题目数量，1-20",
          "minimum": 1,
          "maximum": 20
        },
        "judging_mode": {
          "type": "string",
          "enum": ["Ground_Truth评测", "基于Prompt评测"],
          "description": "评判模式，默认Ground_Truth评测"
        }
      },
      "required": ["description", "question_count"]
    }
  }
}
```

#### 执行函数对接

| 步骤 | 对接 | 位置 |
|---|---|---|
| 1. 构造生成 prompt | 内联（见 3.1.1） | 工具函数内 |
| 2. 调 LLM 生成题目 | `chat_completion(system, user, model_key)` | `chat/llm_router.py`（阶段1） |
| 3. 解析 JSON | `parse_json_loose(content)` | `chat/llm_router.py`（阶段1） |
| 4. 补全字段 | `normalize_dataset_items(data)` | `services/dataset_items.py:45-62` |
| 5. 落库 Dataset | 直接构造 ORM 写入 | `database.py:40-47` Dataset 模型 |

> **免 HTTP**：工具函数同进程直接 `session.add(Dataset(...))`，不走 `POST /upload`。与阶段 2 工具集一致。

#### 3.1.1 生成 Prompt 设计

```
你是评测数据集生成专家。根据需求生成评测题。

需求：{description}
题目数量：{question_count}
评判模式：{judging_mode}

输出 JSON 数组，每题含：
{
  "question": "问题文本",
  "context": "上下文（可选，供忠实度评测）",
  "ground_truth": "标准答案（Ground_Truth评测时必填）",
  "model_answer": "",
  "judging_mode": "{judging_mode}",
  "scenario_key": null
}

要求：
1. 问题多样化，覆盖不同难度
2. ground_truth 准确可验证
3. 只输出 JSON 数组，不要额外解释
```

#### 返回给 agent loop

```json
{
  "dataset_id": "ds_a1b2c3",
  "name": "本体知识问答v1",
  "item_count": 10,
  "preview": [
    {"question": "本体是什么？", "ground_truth": "..."}
  ]
}
```

> 预览只含前 3 题，避免 tool_result 过大。完整题目用户到"评测管理"页面查看。

#### 安全约束

- `question_count` 上限 20（防止 LLM 调用费用失控 + 响应超时）
- 生成失败（LLM 输出非法 JSON）返回错误给 agent loop，不落库空数据集
- 生成的数据集 `name` 冲突时自动追加 `_v2`/`_v3` 后缀

---

### 3.2 `generate_scenario_template` — 生成场景子步骤模板

#### Schema

```json
{
  "type": "function",
  "function": {
    "name": "generate_scenario_template",
    "description": "为指定数据集题目生成场景子步骤评测模板。LLM 分析题目后生成子步骤（step_key/name/weight/gt_criteria），落库为场景模板。替代手工在 ScenarioItemStepsPanel 配置子步骤。",
    "parameters": {
      "type": "object",
      "properties": {
        "dataset_id": {
          "type": "string",
          "description": "数据集 ID"
        },
        "dataset_item_id": {
          "type": "string",
          "description": "题目 ID（数据集内某题）。留空则为整个数据集生成通用模板"
        },
        "question_text": {
          "type": "string",
          "description": "题目原文（LLM 据此设计子步骤）"
        },
        "step_count": {
          "type": "integer",
          "description": "期望子步骤数量，2-5，默认3",
          "minimum": 2,
          "maximum": 5
        }
      },
      "required": ["dataset_id", "question_text"]
    }
  }
}
```

#### 执行函数对接

| 步骤 | 对接 | 位置 |
|---|---|---|
| 1. 构造生成 prompt | 内联 | 工具函数内 |
| 2. 调 LLM 生成子步骤 | `chat_completion` | `chat/llm_router.py` |
| 3. 解析 JSON | `parse_json_loose` | `chat/llm_router.py` |
| 4. 落库模板+子步骤 | `upsert_item_template(...)` | `services/dataset_item_scenario_service.py:84-155` |

> **复用 `upsert_item_template`**：该函数已含 upsert（新建或覆盖）+ `ensure_step_match_rules` 校验 + `validate_llm_aggregate_step` 校验，无需重写落库逻辑。

#### 生成 Prompt 设计

```
你是场景评测模板设计专家。根据题目生成子步骤评测模板。

题目：{question_text}
期望子步骤数：{step_count}

参照通用三层模板 generic_l0 的设计：
- L0-1 理解题意：正确理解问题与约束（weight 0.25, required）
- L0-2 推理与工具：调用正确工具完成推理（weight 0.50, required）
- L0-3 生成答案：输出格式正确且完整的答案（weight 0.25, required）

输出 JSON 数组，每个子步骤含：
{
  "step_key": "step_1",
  "name": "子步骤名称",
  "step_order": 1,
  "weight": 0.33,
  "required": true,
  "enforce_order": true,
  "gt_criteria": ["评判标准1", "评判标准2"],
  "gt_expected": {"keywords": ["关键词1"]},
  "match_rules": {"eval_mode": "keyword"}
}

要求：
1. 子步骤覆盖从理解到生成答案的完整链路
2. weight 之和为 1.0
3. required 步骤至少 2 个
4. gt_criteria 用可验证的具体标准
5. 只输出 JSON 数组
```

#### 返回给 agent loop

```json
{
  "template_id": "scen_xxx_v1",
  "scenario_key": "ditem_xxx",
  "step_count": 3,
  "steps_preview": [
    {"step_key": "step_1", "name": "理解题意", "weight": 0.33}
  ]
}
```

---

### 3.3 `tune_judge_prompt` — 优化 G-Eval 评判标准

#### Schema

```json
{
  "type": "function",
  "function": {
    "name": "tune_judge_prompt",
    "description": "优化自定义指标（G-Eval 类型）的评判标准 criteria。读取现有 criteria，LLM 改写得更清晰可判分。不改动其他字段。",
    "parameters": {
      "type": "object",
      "properties": {
        "custom_metric_id": {
          "type": "integer",
          "description": "自定义指标 ID（CustomMetric 表 id）"
        },
        "improvement_hint": {
          "type": "string",
          "description": "优化方向提示，如'更严格''增加边界情况''减少歧义'。留空则通用优化"
        }
      },
      "required": ["custom_metric_id"]
    }
  }
}
```

#### 执行函数对接

| 步骤 | 对接 | 位置 |
|---|---|---|
| 1. 读现有 criteria | `select(CustomMetric).where(id==...)` | `database.py:674` CustomMetric |
| 2. 校验 kind=geval | 工具函数内 | — |
| 3. 构造改写 prompt | 内联 | — |
| 4. 调 LLM 改写 | `chat_completion` | `chat/llm_router.py` |
| 5. 更新 criteria | `session.execute(update(CustomMetric)...)` | 同进程直写 |

> **免 HTTP**：直接更新 `CustomMetric.criteria` 字段，不走 `PUT /custom-metrics/{id}`。但需调用相同的校验 `_validate_custom_metric_fields`（`main.py:719-726`）确保 criteria 非空。

#### 改写 Prompt 设计

```
你是 LLM 评测 prompt 工程专家。优化以下 G-Eval 评判标准，使其更清晰、更可判分。

原始 criteria：
{original_criteria}

优化方向：{improvement_hint or "通用优化：更清晰、减少歧义、覆盖边界情况"}

优化要求：
1. 保持原意，不改变评判维度
2. 用可操作的具体标准替代模糊表述
3. 明确高分/低分的边界
4. 控制在 200 字以内
5. 只输出优化后的 criteria 文本，不要解释
```

#### 返回给 agent loop

```json
{
  "custom_metric_id": 3,
  "name": "回答完整性",
  "original_criteria": "回答应完整覆盖问题要点...",
  "optimized_criteria": "回答须覆盖以下要点：(1)...(2)...；缺失任一要点扣...",
  "applied": true
}
```

> `applied: true` 表示已落库更新。LLM 收到后可告诉用户"已优化指标'回答完整性'的评判标准，请到指标管理查看"。

#### 安全约束

- 仅 `kind=geval` 的指标可优化（dag/code 结构复杂，LLM 不碰）
- 原始 criteria 留存于返回值，便于回滚（用户可手工改回）
- 优化后 criteria 为空时拒绝更新，保留原值

---

### 3.4 `recommend_verdict_thresholds` — 推荐四维阈值

#### Schema

```json
{
  "type": "function",
  "function": {
    "name": "recommend_verdict_thresholds",
    "description": "根据任务的评测历史数据，推荐四维结论阈值调整建议。分析各维度实际分布，推荐更合理的 pass/warn 阈值。",
    "parameters": {
      "type": "object",
      "properties": {
        "task_id": {
          "type": "string",
          "description": "评测任务 ID（取历史数据）"
        },
        "apply": {
          "type": "boolean",
          "description": "是否直接应用推荐值到任务阈值。false（默认）仅返回建议不落库；true 落库为任务级阈值覆盖"
        }
      },
      "required": ["task_id"]
    }
  }
}
```

#### 执行函数对接

| 步骤 | 对接 | 位置 |
|---|---|---|
| 1. 读当前阈值 | `select(EvaluationTask.verdict_thresholds)` | `database.py:96` |
| 2. 读历史结果 | `select(EvaluationResult).where(task_id==...)` 聚合分布 | `database.py:106` |
| 3. 构造推荐 prompt | 内联（含当前阈值 + 历史分布统计） | — |
| 4. 调 LLM 推荐 | `chat_completion` | `chat/llm_router.py` |
| 5. 解析 JSON | `parse_json_loose` | `chat/llm_router.py` |
| 6. (apply=true) 落库 | 写 `EvaluationTask.verdict_thresholds` JSON | `database.py:96` |

> **默认不自动应用**：`apply` 默认 false，仅返回建议。用户在前端确认后才 `apply=true` 调一次落库。与阶段 1 的"草稿需人工确认启动"安全理念一致。

#### 阈值字段清单（13 个）

源自 `services/verdict_threshold_service.py:27-41` 和 `dimension_verdict_constants.py:1-19`：

| 字段 | 维度 | 默认值 | 值域 |
|---|---|---|---|
| `quality_pass_rate_pass` | 质量指标 | 0.80 | 0-1 |
| `quality_pass_rate_warn` | 质量指标 | 0.60 | 0-1 |
| `quality_total_pass` | 质量总分 | 0.80 | 0-1 |
| `quality_total_warn` | 质量总分 | 0.60 | 0-1 |
| `process_chain_pass_warn` | 过程一致性 | 0.60 | 0-1 |
| `scenario_process_pass` | 场景交付 | 0.80 | 0-1 |
| `scenario_process_warn` | 场景交付 | 0.60 | 0-1 |
| `scenario_required_pass` | 场景交付 | 0.80 | 0-1 |
| `scenario_required_warn` | 场景交付 | 0.60 | 0-1 |
| `scenario_score_std_volatile` | 场景交付 | 0.05 | 0-1 |
| `struct_drift_ratio_significant` | 结构漂移 | 0.30 | (0,1] |
| `perf_pass_rate_within` | 运行稳定性 | 80.0 | 0-100 |
| `perf_pass_rate_elevated` | 运行稳定性 | 60.0 | 0-100 |

#### 推荐 Prompt 设计

```
你是评测阈值调优专家。根据任务历史数据，推荐四维结论阈值调整。

当前阈值：
{current_thresholds_json}

历史数据分布（最近 N 轮）：
- 质量通过率分布：P50={q_p50}, P90={q_p90}
- 质量总分分布：P50={t_p50}, P90={t_p90}
- 场景子步骤通过率分布：P50={s_p50}
- 推理延迟通过率分布：P50={p_p50}

推荐原则：
1. pass 阈值应接近 P75 分位（让约 75% 的用例通过）
2. warn 阈值应低于 pass（差距 0.15-0.20）
3. 不要剧烈偏离当前值（单次调整幅度 <0.15）
4. 仅推荐需要调整的字段，未调整的不输出

输出 JSON：
{
  "recommended": {"quality_pass_rate_pass": 0.75, ...},
  "reason": "质量通过率 P75=0.78，当前 pass=0.80 偏严，建议降至 0.75"
}
```

#### 返回给 agent loop

```json
{
  "task_id": "task_xxx",
  "current": {"quality_pass_rate_pass": 0.80, ...},
  "recommended": {"quality_pass_rate_pass": 0.75, "quality_pass_rate_warn": 0.55},
  "reason": "质量通过率 P75=0.78，当前 pass=0.80 偏严...",
  "applied": false
}
```

> 前端可渲染为对比表（当前 vs 推荐），用户确认后发第二次 tool_call `apply=true`。

---

## 4. 工具注册与 system prompt 更新

### 4.1 注册到 orchestrator

阶段 2 的 `orchestrator.py` 维护一个工具清单列表，阶段 3 只需追加 4 个 schema：

```python
# chat/tools.py 末尾追加

GENERATION_TOOLS = [
    GENERATE_DATASET_SCHEMA,
    GENERATE_SCENARIO_SCHEMA,
    TUNE_JUDGE_PROMPT_SCHEMA,
    RECOMMEND_VERDICT_THRESHOLDS_SCHEMA,
]

# orchestrator.py 中合并
ALL_TOOLS = CONFIG_TOOLS + GENERATION_TOOLS  # 7 + 4 = 11 个
```

### 4.2 system prompt 追加

在阶段 2 的 system prompt 末尾追加生成类工具说明：

```
生成类工具（按需调用，生成专业配置产物）：
- generate_dataset_from_description: 根据需求描述生成评测数据集
- generate_scenario_template: 为题目生成场景子步骤模板
- tune_judge_prompt: 优化 G-Eval 自定义指标的评判标准
- recommend_verdict_thresholds: 推荐四维结论阈值

规则：
- 用户明确要求"生成/创建/优化/推荐"时才调用生成类工具
- 生成类工具会消耗较多 LLM token，不要频繁调用
- 生成后用自然语言告知用户产物 ID 和下一步操作
- recommend_verdict_thresholds 默认不自动应用（apply=false），用户确认后再调 apply=true
```

---

## 5. 前端

**无新增组件**。阶段 2 的 `ToolCallBubble` 复用：

- 生成类工具的 `chat_tool_call` / `chat_tool_result` 事件用同一个 `ToolCallBubble` 渲染
- `recommend_verdict_thresholds` 返回的当前 vs 推荐对比，由 LLM 在 `chat_message` 中用文本说明，或前端解析 `tool_result.recommended` 渲染（可选增强，初版 LLM 文本说明即可）

**唯一前端改动**（可选）：`EvalDraftPreviewCard` 增加"查看生成产物"快捷链接——当 `tool_result` 含 `dataset_id` 时，卡片显示"数据集 ds_xxx 已生成 → 前往评测管理"链接。这是 UX 增强，非必须。

---

## 6. 数据模型

**无新增表，无迁移**。生成的产物落库到现有表：

| 工具 | 落库目标表 | 字段 |
|---|---|---|
| `generate_dataset_from_description` | `datasets` | id/name/data(JSON)/item_count |
| `generate_scenario_template` | `scenario_eval_templates` + `scenario_eval_steps` | 模板 + 子步骤 |
| `tune_judge_prompt` | `custom_metrics` | criteria 字段更新 |
| `recommend_verdict_thresholds` | `evaluation_tasks` | verdict_thresholds JSON |

---

## 7. 实施任务清单

### 7.1 后端

| # | 任务 | 产出 | 验收 |
|---|---|---|---|
| B3-1 | `generate_dataset_from_description` 工具函数 + schema | `chat/tools.py` 追加 | mock LLM 返回题目 JSON → 落库 Dataset → 查询验证 |
| B3-2 | `generate_scenario_template` 工具函数 + schema | `chat/tools.py` 追加 | mock LLM → 调 `upsert_item_template` → 查询 ScenarioEvalStep |
| B3-3 | `tune_judge_prompt` 工具函数 + schema | `chat/tools.py` 追加 | mock LLM → 更新 CustomMetric.criteria → 查询验证 |
| B3-4 | `recommend_verdict_thresholds` 工具函数 + schema | `chat/tools.py` 追加 | mock 历史 EvaluationResult → LLM 推荐 → apply=true 落库 |
| B3-5 | 工具注册 + system prompt 追加 | `orchestrator.py` 修改 | 11 个工具均对 LLM 可见 |

### 7.2 前端

| # | 任务 | 产出 | 验收 |
|---|---|---|---|
| F3-1 | (可选) EvalDraftPreviewCard 加"查看生成产物"链接 | `EvalDraftPreviewCard.tsx` 增强 | tool_result 含 dataset_id 时显示链接 |
| F3-2 | (可选) 阈值推荐对比渲染 | `ChatPage.tsx` | recommend 工具结果渲染当前 vs 推荐表 |

> 前端任务标记"可选"，初版由 LLM 文本说明产物，不强制做专属渲染。

### 7.3 测试与联调

| # | 任务 | 验收 |
|---|---|---|
| T3-1 | `generate_dataset_from_description`：描述"生成5道本体知识问答" | 生成 5 题，Dataset 落库，item_count=5，题目合理 |
| T3-2 | `generate_scenario_template`：给定题目"请解释本体映射" | 生成 3 子步骤，weight 和=1，落库 ScenarioEvalStep |
| T3-3 | `tune_judge_prompt`：指定 G-Eval 指标 + hint"更严格" | criteria 改写更具体，CustomMetric 更新成功 |
| T3-4 | `recommend_verdict_thresholds`：有历史数据的 task | 推荐阈值在合理范围，apply=true 后 task.verdict_thresholds 更新 |
| T3-5 | 端到端对话："帮我生成10道评测题并评估 agent-001" | LLM 链式调 generate_dataset → list_agents → build_eval_request |
| T3-6 | 安全：generate_dataset question_count=50 超限 | 拒绝或截断到 20 |
| T3-7 | 安全：tune_judge_prompt 指定 dag/code 类型指标 | 拒绝，提示"仅支持 geval 类型" |

---

## 8. 验收标准

1. 用户对话说"生成 10 道本体知识评测题"，LLM 调用 `generate_dataset_from_description`，生成合理题目并落库，返回 dataset_id
2. 用户对话说"给这道题配场景子步骤"，LLM 调用 `generate_scenario_template`，生成子步骤落库，用户到 ScenarioItemStepsPanel 可见
3. 用户对话说"优化这个 G-Eval 指标的评判标准，更严格些"，LLM 调用 `tune_judge_prompt`，criteria 更新，CustomMetricManager 可见
4. 用户对话说"这个任务的阈值合理吗"，LLM 调用 `recommend_verdict_thresholds`（apply=false），返回建议；用户说"应用吧"，再调一次（apply=true）落库
5. 链式调用：用户说"生成评测集并评估 agent-001"，LLM 先 generate_dataset 再 build_eval_request，草稿含新 dataset_id
6. 安全约束全部生效（question_count 上限、仅 geval 可优化、阈值默认不自动应用）
7. 阶段 1/2 功能零回归

---

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| LLM 生成的评测题质量低/有幻觉 | 生成后提示用户"到评测管理查看/微调"；不直接用于正式评测，定位为原型生成 |
| LLM 生成子步骤 gt_criteria 不可验证 | prompt 强制"可验证的具体标准"；`upsert_item_template` 的 `ensure_step_match_rules` 校验拦截非法结构 |
| `tune_judge_prompt` 改写后判分偏移 | 保留 original_criteria 在返回值，支持用户手工改回；建议改写后用 `/custom-metrics/{id}/test` 试运行验证 |
| `recommend_verdict_thresholds` 推荐不当导致结论翻转 | 默认 `apply=false` 仅建议；apply=true 需用户二次确认；推荐幅度限制 <0.15 |
| 生成类工具 token 消耗大 | system prompt 限制"按需调用，不频繁"；question_count 上限 20 |
| 嵌套 LLM 调用死锁 | 生成类工具内的 `chat_completion` 走 8 并发信号量，与 agent loop 外层 LLM 共享池；超时 180s |
| 历史数据不足时推荐无意义 | task 无历史结果时返回"数据不足，无法推荐" |

---

## 10. 关键文件索引

| 用途 | 路径 | 行号 |
|---|---|---|
| Dataset 模型（生成评测集落库） | `backend/database.py` | 40-47 |
| DatasetItem schema（生成题目字段） | `backend/schemas.py` | 15-36 |
| normalize_dataset_items（补全字段） | `backend/services/dataset_items.py` | 45-62 |
| ScenarioEvalTemplate 模型 | `backend/database.py` | 484-499 |
| ScenarioEvalStep 模型 | `backend/database.py` | 502-527 |
| upsert_item_template（场景模板落库） | `backend/services/dataset_item_scenario_service.py` | 84-155 |
| generic_l0 模板参考 | `backend/services/scenario_eval_service.py` | 22-53 |
| CustomMetric 模型 | `backend/database.py` | 674-695 |
| _validate_custom_metric_fields | `backend/main.py` | 719-726 |
| CustomMetric CRUD 路由 | `backend/main.py` | 729-847 |
| verdict_thresholds 字段 | `backend/database.py` | 96 |
| VerdictThresholds dataclass（13字段） | `backend/services/verdict_threshold_service.py` | 27-41 |
| 四维阈值默认常量 | `backend/services/dimension_verdict_constants.py` | 1-19 |
| PATCH /tasks/{id}/verdict-thresholds | `backend/main.py` | 2212-2258 |
| 阶段 2 orchestrator（工具注册处） | `backend/chat/orchestrator.py` | 待实施 |
| 阶段 2 tools.py（追加位置） | `backend/chat/tools.py` | 待实施 |
| 阶段 1 llm_router（生成工具内调 LLM） | `backend/chat/llm_router.py` | 已实施 |
| 前端 EvalDraftPreviewCard（可选增强） | `frontend/src/components/EvalDraftPreviewCard.tsx` | 已实施 |
