# 评测维度重构设计：评测指标单 tab + 类别内分组 + 聚合桶隔离

## 文档信息

| 项目 | 内容 |
|------|------|
| 文档名称 | 评测指标维度重构 — 单 tab 类别分组 + 聚合桶隔离 |
| 版本 | v0.2（待评审） |
| 状态 | 设计阶段，待 review 后进入实现 |
| 日期 | 2026-07-30 |
| 修订说明 | v0.2：从"五维强拆"改为"评测指标单 tab + 类别内折叠分组 + 聚合桶隔离"。结论卡保持四卡，安全护栏在卡内用 chip 呈现而非另开一卡；明细 tab 内按 6 个 DeepEval category 分组。 |
| 关联 | [onto-eval评测优化架构设计说明书.md](./onto-eval评测优化架构设计说明书.md) §22 |

---

## 1. 背景与问题

平台原有四维裁决：质量指标 / 过程一致性 / 场景交付 / 运行稳定性。引入 DeepEval 25+ 指标与自定义指标后出现两个问题：

**问题 A（业务语义错位）**：[quality_score_service.py:49](../backend/services/quality_score_service.py#L49-L56) `_is_quality_metric()` 仅排除 `inference_latency / tokens_total`，导致安全护栏（lower_is_better）、工具调用、规划推理、多轮对话、自定义指标**全部**进输出质量加权池。安全类 `lower_is_better` 与质量类 `higher_is_better` 混池加权，语义和数学都错。

**问题 B（布局体验差）**：[EvalDetailModal.tsx:642](../frontend/src/components/EvalDetailModal.tsx#L641-L651) "质量指标"tab 把所有非运行时指标平铺成一张表，标题"质量指标概览/答案内容质量"名不副实；评估因子选择器按七八个细粒度 category 平铺分组，视觉噪音大。

### 关键盲区：缺"过程能力"语义

规划推理类指标（step_efficiency/plan_adherence/plan_quality）本质是**单次过程质量**，既非"跨轮稳定性"（过程一致性）也非"时延/Token 成本"（运行效能）：

- 过程一致性 = **跨轮稳定性**（同一题多轮跑，必达点/链条是否过）→ 回答"稳不稳"
- 运行效能 = **成本/速度**（时延 ms、Token 数）→ 回答"快不快、贵不贵"
- 规划推理 = **单次过程质量**（计划好不好、步骤有没有绕路、有没有照计划走）→ 回答"这次想得对不对"

工具类（task_completion/tool_correctness/argument_correctness）同属"过程能力"。硬塞进过程一致性或运行效能都会错位。

---

## 2. 方案总览：四层模型

| 层 | 作用 | 决定什么 |
|----|------|---------|
| ① 语义维度 | 业务评判口径 | 指标归到哪个评判语义（输出质量/安全/过程能力/...） |
| ② 聚合桶 | 后端裁决逻辑 | 是否进质量加权分、怎么计分、写哪张卡 |
| ③ tab 分组 | 详情页明细组织 | "评测指标"tab 内折叠分组的标题 |
| ④ 结论卡 | 四维 pass/fail 卡 | 卡片数量、命名、护栏 chip |

> 指标 → 语义维度 → 聚合桶（驱动后端） / → tab 分组（驱动前端明细）。

---

## 3. 第一层：指标 → 语义维度

| 语义维度 | 回答什么 | 计分方向 | 归口指标 |
|----------|---------|----------|---------|
| 输出质量 | 答得好不好（切题/正确/忠实/格式/检索是否支撑回答） | higher_is_better | correctness, answer_relevancy, faithfulness, golden_match, hallucination, prompt_alignment, json_correctness, contextual_precision/recall/relevancy |
| 安全合规 | 有没有越红线 | lower_is_better | bias, toxicity, pii_leakage, role_violation, non_advice, misuse |
| 过程能力 | 想得对不对、走得顺不顺（单次执行） | higher_is_better | task_completion, tool_correctness, argument_correctness, step_efficiency, plan_adherence, plan_quality |
| 多轮交付 | 多轮聊得好不好 | higher_is_better | conversation_completeness, knowledge_retention, role_adherence, turn_relevancy, goal_accuracy, tool_use, topic_adherence |
| 跨轮稳定性 | 同一题多轮跑稳不稳 | 描述性 | （原过程一致性逻辑：必达点/链条通过率，非 LLM 指标） |
| 运行成本 | 快不快、贵不贵 | lower_is_better | inference_latency, tokens_total |

---

## 4. 第二层：聚合桶（后端裁决逻辑）

| 聚合桶 key | 归口语义维度 | 参与质量总分? | 计分方式 | 呈现位置 |
|-----------|-------------|--------------|---------|---------|
| `output_quality` | 输出质量 | ✅ 加权平均（hallucination 翻转后参与） | `_compute_result_score` | 评测指标卡主分 |
| `guardrail` | 安全合规 | ❌ 不进加权 | 通过率 + 零容忍 gate | 评测指标卡内护栏 chip |
| `process_capability` | 过程能力 | ❌ 不进加权 | 通过率（补充信息） | 过程一致性卡 `supplementary` |
| `multi_turn` | 多轮交付 | ❌ 不进加权 | 通过率（补充信息） | 场景交付卡 `supplementary` |
| `runtime` | 运行成本 | ❌ | 达标率 | 运行稳定性卡 |

> **核心修复**：`_is_quality_metric()` → 换成 `_metric_aggregation_bucket(row)`，只对 `output_quality` 桶返回参与加权。安全护栏不再污染输出质量分，过程能力/多轮交付不再被塞进质量加权池。

### 4.1 output_quality 桶计分

`QUALITY_METRIC_WEIGHTS`（answer_quality + rag 类）：

```python
QUALITY_METRIC_WEIGHTS = {
    "correctness": 0.25, "answer_relevancy": 0.20, "faithfulness": 0.20,
    "golden_match": 0.15, "hallucination": 0.10,        # lower_is_better，翻转 1-score
    "contextual_precision": 0.10, "contextual_recall": 0.10,
    "contextual_relevancy": 0.10, "prompt_alignment": 0.10, "json_correctness": 0.10,
}
# 未在表中的 output_quality 指标按维度内均分剩余权重
```

### 4.2 guardrail 桶计分（安全护栏）

```python
# 每个安全指标按 gate_threshold 判 passed（score <= threshold → passed）
# 维度级状态：
#   任一指标 score > 1.5× gate_threshold（硬违规）→ fail（零容忍红线）
#   任一指标通过率 < 80% → fail
#   全部通过率 >= 95% → pass
#   其余 → warn
# headline 例：「PII 泄露 2 例 ⚠ / bias 0 违规 / 共 50 题检测」
# 对质量总分 gate：guardrail=fail 时质量总分封顶 0.6（可配开关，默认开）
```

### 4.3 process_capability / multi_turn 桶

通过率作为补充信息写入对应卡的 `metrics.supplementary_metric_pass_rates`，**不**覆盖主状态/主分（过程能力是"这一轮想得对不对"，过程一致性是"多轮稳不稳"，二者语义不同，不应混算）。

---

## 5. 第三层：tab 分组（详情页明细）

复用 [deepeval_suite.py:53](../backend/evaluators/deepeval_metrics/deepeval_suite.py#L53-L60) 现有 6 个 `CATEGORY_LABELS` + 自定义集合，作为"评测指标"tab 内折叠分组：

| tab 内分组 | 来源 category | 聚合桶 | 说明 |
|-----------|--------------|--------|------|
| 回答质量(切题/正确/格式) | answer_quality | output_quality | correctness, golden_match, hallucination, prompt_alignment, json_correctness |
| 检索RAG(需检索上下文) | rag | output_quality | faithfulness, contextual_precision/recall/relevancy |
| 安全合规(偏见/毒性/隐私/越界) | safety | guardrail | 红色护栏标记 |
| 工具调用(工具选择/参数/完成度) | tool | process_capability | |
| 规划推理(计划/步骤过程) | reasoning | process_capability | |
| 多轮对话(需对话回合) | conversation | multi_turn | |
| 自定义·{集合名} | custom | 按 category 归口 | |

### 明细 tab UI 形态

```
评测指标                                          [全展开 ▼][全收起]
├ ▾ 回答质量(切题/正确/格式)     均分 0.82  通过 90%   5项
│    答案相关性  0.85 ...   金标对照 0.78 ...
├ ▾ 安全合规(偏见/毒性/隐私)     ⚠ 违规 2例 通过 96%   6项
│    偏见 0.02 ...   PII泄露 0.08 ...
├ ▸ 工具调用                     均分 0.75  通过 80%   3项
├ ▸ 规划推理                     均分 0.70  通过 70%   3项
├ ▸ 检索RAG                      均分 0.88  通过 95%   4项
└ ▸ 自定义·化工安全集              均分 0.60  通过 60%   2项
```

---

## 6. 第四层：结论卡（保持四卡）

| 卡 | 命名 | 内容 |
|----|------|------|
| 评测指标（原"质量指标"，放大命名） | status/score 来自 output_quality 桶；**护栏 chip** 来自 guardrail 桶（绿=无违规/红=有违规） | headline：质量分 + 护栏状态 |
| 过程一致性 | 原逻辑（必达点/链条通过率）+ process_capability 桶通过率作补充 | 不破坏原状态 |
| 场景交付 | 原逻辑（场景子步骤）+ multi_turn 桶通过率作补充 | 不破坏原状态 |
| 运行稳定性 | 不变 | |

护栏 chip 形态（评测指标卡内右上角）：

```
┌ 评测指标 — 达标 ────── 🛡 护栏:⚠ 有违规 ┐
│ 结果分 0.82  检索召回 100%                │
│ PII泄露 2例 / bias 0违规                   │
└──────────────────────────────────────────┘
```

---

## 7. 数据模型与后端变更

### 7.1 无需数据库迁移

- `MetricSpec`（声明式内存对象）加 `aggregation_bucket` 字段，从 `category` 派生。
- `CustomMetric.category` 复用为聚合桶来源。
- `EvaluationResult` 不加列——桶在裁决读模型按 metric_key 动态解析，历史数据零迁移。

### 7.2 新增 `MetricSpec.aggregation_bucket`

[deepeval_suite.py](../backend/evaluators/deepeval_metrics/deepeval_suite.py)：

```python
CATEGORY_TO_AGGREGATION_BUCKET = {
    "answer_quality": "output_quality",
    "rag": "output_quality",
    "safety": "guardrail",
    "tool": "process_capability",
    "reasoning": "process_capability",
    "conversation": "multi_turn",
}

@dataclass(frozen=True)
class MetricSpec:
    ...
    aggregation_bucket: str = ""  # 空 → 从 category 派生

    @property
    def effective_bucket(self) -> str:
        return self.aggregation_bucket or CATEGORY_TO_AGGREGATION_BUCKET.get(self.category, "output_quality")
```

### 7.3 维度解析函数（运行时单一入口）

```python
# services/aggregation_bucket_resolver.py（新增）
RUNTIME_BUCKET_KEYS = {"inference_latency", "tokens_total"}

def resolve_metric_bucket(metric_key: str, custom_category: str | None = None) -> str:
    """任意 metric_key → aggregation_bucket。运行时解析，不依赖 DB 列。"""
    if metric_key in RUNTIME_BUCKET_KEYS:
        return "runtime"
    spec = SPEC_BY_KEY.get(metric_key)
    if spec:
        return spec.effective_bucket
    # 自定义：按 category 归口，非五桶 key 回退 output_quality
    if custom_category in CATEGORY_TO_AGGREGATION_BUCKET.values():
        return custom_category
    return "output_quality"
```

### 7.4 quality_score_service.py 变更

[quality_score_service.py](../backend/services/quality_score_service.py)：

1. `_is_quality_metric(row)` → `_metric_bucket(row) -> str`（调 `resolve_metric_bucket`）。
2. `_compute_result_score` / `_compute_result_scores`：只聚合 `bucket == 'output_quality'` 的行。
3. `METRIC_WEIGHTS` → `QUALITY_METRIC_WEIGHTS`（§4.1）；未知指标按维度内均分。
4. `_compute_quality_stability`：稳定性统计范围改为 `QUALITY_METRIC_WEIGHTS` 的 keys。

### 7.5 task_dimension_verdict_service.py 变更

[task_dimension_verdict_service.py](../backend/services/task_dimension_verdict_service.py)：

1. `_load_quality_metric_stats`：过滤条件加 `bucket == 'output_quality'`。
2. 新增 `_load_guardrail_stats(session, task_id)`：聚合 `bucket == 'guardrail'` 的行，按指标算通过率 + 违规数 + 硬违规标记。
3. 新增 `_guardrail_verdict(stats, thr)`：§4.2 逻辑，返回 `{status, violations, headline, per_metric}`。
4. `_verdict_quality` 增加 `guardrail` 子字段：

```python
quality = _verdict_quality(quality_score, metric_stats, thr)
quality["guardrail"] = _guardrail_verdict(guardrail_stats, thr)
# 质量总分 gate：guardrail.status == 'fail' 时封顶 0.6
```

5. process_capability / multi_turn 桶通过率写入对应卡 `metrics.supplementary_metric_pass_rates`。

### 7.6 API 契约变更

[/metrics](../backend/main.py#L271-L354) 返回增加 `aggregation_bucket`：

```python
# deepeval 项
"aggregation_bucket": spec.effective_bucket,
# custom 项
"aggregation_bucket": resolve_metric_bucket(cm.key, cm.category),
# 顶层
"aggregation_buckets": {
    "output_quality": "输出质量", "guardrail": "安全护栏",
    "process_capability": "过程能力", "multi_turn": "多轮交付", "runtime": "运行成本",
},
```

`format_dimension_verdicts_for_summary` 增加护栏一行。

---

## 8. 前端变更

### 8.1 类型定义

[dimensionVerdict.ts](../frontend/src/types/dimensionVerdict.ts)：quality 维度增加 `guardrail` 子结构：

```typescript
quality: {
  status: VerdictStatus
  score: number | null
  headline: string
  tab_id: 'quality'
  guardrail?: {                    // 🆕
    status: 'pass' | 'warn' | 'fail'
    violations: number
    headline: string
    per_metric?: Record<string, { pass_rate: number | null; violations: number }>
  }
  metrics: Record<string, unknown>
}
```

### 8.2 结论卡 DimensionVerdictBar

[DimensionVerdictBar.tsx](../frontend/src/components/DimensionVerdictBar.tsx)：

- "质量指标"卡 → 改名"**评测指标**"。
- 卡内右上角渲染护栏 chip：绿盾（无违规）/ 红盾（有违规），点击可跳到明细 tab 安全分组。
- 保持四卡 grid 布局不变。

### 8.3 详情 tab

[EvalDetailModal.tsx:641-651](../frontend/src/components/EvalDetailModal.tsx#L641-L651) `mainTabs`：

- `label: '质量指标'` → `label: '评测指标'`。
- [EvalDetailModal.tsx:1062](../frontend/src/components/EvalDetailModal.tsx#L1062) "质量指标概览" → "评测指标概览"，副标题去掉"答案内容质量"限定。
- 明细表按 §5 的 category 分组渲染为折叠 section（每组带均分/通过率小计），替代当前扁平单表。

### 8.4 评估因子选择器

[EvalPage.tsx:317-350](../frontend/src/components/EvalPage.tsx#L317-L350) ComboDropdown：按 `aggregation_bucket` 中文名二级分组（输出质量/安全护栏/过程能力/多轮交付/运行成本），safety 组标红"越低越好·零容忍"。

### 8.5 指标库 MetricsCatalog

[MetricsCatalog.tsx](../frontend/src/components/MetricsCatalog.tsx)：保留按 category 分组；增加"按聚合桶"视图切换 Tab。

### 8.6 自定义指标表单

[CustomMetricManager.tsx:360-375](../frontend/src/components/CustomMetricManager.tsx#L360-L375) "指标类别"输入框 → "归属聚合桶"下拉（output_quality/guardrail/process_capability/multi_turn）。

---

## 9. 兼容性与回退

| 场景 | 处理 |
|------|------|
| 历史 EvaluationResult 含安全指标 | 按 metric_key 动态解析为 guardrail 桶，不再进质量加权（历史质量分会下降，符合预期） |
| 历史任务无 guardrail 子字段 | quality.guardrail 缺失 → 前端回退不显示 chip |
| 自定义指标 category 非桶 key | 回退 output_quality |
| 离线任务 | guardrail 桶可评（吃 input/output）；process_capability/multi_turn 多依赖 trace → NA |

---

## 10. 测试要点

1. **桶映射正确性**：每个 METRIC_SPECS 的 `effective_bucket` 断言。
2. **质量分隔离**：选 bias + answer_relevancy，质量分只含后者。
3. **护栏裁决**：bias 硬违规 → guardrail.status=fail + 质量总分 gate 封顶 0.6。
4. **离线护栏可评**：离线任务选 bias，guardrail 非空。
5. **过程能力不污染质量分**：选 step_efficiency，不进质量加权池。
6. **明细 tab 分组**：6 个 category 折叠 section 正确渲染，safety 组红色标记。
7. **四卡护栏 chip**：评测指标卡显示绿/红盾，点击跳明细安全分组。
8. **历史兼容**：历史 task verdict payload 不报错，guardrail 缺失回退。

---

## 11. 落地步骤

| 步骤 | 范围 | 依赖 |
|------|------|------|
| S1 | `MetricSpec.aggregation_bucket` + `resolve_metric_bucket()` + 单测 | 无 |
| S2 | `quality_score_service` 按 bucket 过滤 + `QUALITY_METRIC_WEIGHTS` | S1 |
| S3 | `_guardrail_verdict()` + `_verdict_quality` 增加 guardrail 子结构 + 质量总分 gate | S1,S2 |
| S4 | process_capability/multi_turn 桶通过率写入对应卡 supplementary | S1 |
| S5 | `/metrics` API 返回 aggregation_bucket | S1 |
| S6 | 前端 `dimensionVerdict.ts` 类型 + DimensionVerdictBar 护栏 chip + 卡改名 | S3 |
| S7 | 前端详情 tab 改名 + 明细表按 category 折叠分组 | S5 |
| S8 | 前端评估因子选择器按桶分组 + 指标库桶视图 + 自定义表单桶下拉 | S5 |
| S9 | 回归测试 + 历史兼容验证 | S1-S8 |

建议 S1-S5（后端）先行，S6-S8（前端）跟进。

---

## 12. 待 review 确认点

1. **护栏对质量总分 gate**：guardrail=fail 时质量总分封顶 0.6，是否合理？还是完全独立不联动？
2. **hallucination 翻转**：在 output_quality 桶内 `1-score` 参与加权，对外展示原始分，是否接受？
3. **process_capability/multi_turn 不参与主裁决**：仅作补充信息写 supplementary，不覆盖过程/场景卡状态，是否符合预期？
4. **faithfulness 归属**：放"检索RAG"tab 组（语义贴合）还是"回答质量"组？当前 category=rag → 桶=output_quality + 组=检索RAG。
5. **自定义指标桶下拉**：是否限制只能选桶 key，还是允许自由文本（兼容旧"指标集合"语义）？
