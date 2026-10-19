# onto-agent 评测观测适配方案 v2

> **文档类型**：onto-agent 侧开发方案（含外部 API 契约）  
> **受众**：onto-agent 开发 / 架构 / 联调  
> **关联 eval 文档**：[ST-3-Agent场景步骤埋点-需求规格.md](./ST-3-Agent场景步骤埋点-需求规格.md) · [onto-agent-observability.md](./onto-agent-observability.md) · [onto-agent-最小改动清单.md](./onto-agent-最小改动清单.md) · [openapi/onto-agent-integration.yaml](./openapi/onto-agent-integration.yaml)  
> **版本**：v2.0 · 2026-06-04

---

## 1. 结论摘要（CTO 视角）

**`agent.step.id` 是必要基础，但不足以单独满足当前全部评测诉求。**

| 诉求 | 仅靠 Skill 步骤里写 `agent.step.id` | 还需什么 |
|------|--------------------------------------|----------|
| 场景子步骤 Trace 绑定 | ❌ 不够 | 注册表解析 + metadata 真实落库 + eval `trace_binding.agent_step_id` 配置 + internal_sync 物化 |
| Agent 过程点 | ⚠️ 部分 | `agent.step.kind` / `agent.tool.name` + JSONPath 规则 + 覆盖率 ≥80% |
| Span 树 / 结构 Diff | ⚠️ 部分 | 父子边完整 + `aggregate_key` 稳定 + `agent.tool.call_index` |
| 运行效能（均时延/Tokens） | ❌ 不够 | observation 级 `usage` / `latency` + internal_sync 拉取完整 trace |
| 多版本对比 | ⚠️ 部分 | 跨版本 **稳定** step.id + `agent.version` + registry 版本治理 |
| 稳定性 / 可运维 | ❌ 不够 | internal_sync Worker 物化 + step-id 覆盖度探针 + sanitize 白名单 + 降级可观测 |

**推荐路线**：分四层交付（L0→L3），`agent.step.id` 放在 **L1+L2**，不跳过 L0 与 L3。

---

## 2. 总体架构（可扩展）

```text
┌─────────────────────────────────────────────────────────────────┐
│ L3 质量门禁（联调 / CI）                                         │
│  step-id-report · legacy_aggregate_report · batch-preview-trace │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│ L2 业务步骤语义（ST-3 模式 A）                                   │
│  scenario_steps.yaml → StepResolver → agent.step.id (registry)   │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│ L1 OTel 契约（AG-01）                                            │
│  agent.* metadata · aggregate_key · semantic_role                │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│ L0 事实链路（AG-02 + eval context）                              │
│  sync 推理 · Langfuse flush · internal_sync 物化 · run_span_nodes│
└─────────────────────────────────────────────────────────────────┘
```

**扩展点**（后续可选，本方案预留字段，不阻塞 v2）：

| 扩展 | 预留 |
|------|------|
| 模式 B：eval 下发步骤计划 | `context.eval_step_plan[]` |
| 模式 C：实时步骤指令 | WebSocket / run 级 step hint |
| 确定性 IO 回放 | `agent.is_deterministic_io` + `mock_response_payload` |
| 跨 Agent 类型 | 非 onto-agent 走 HTTP adapter + 同名 metadata 契约 |

---

## 3. 分层交付与验收

### 3.1 L0 — 事实链路（P0，1～2 周）

**目标**：评测 Run 能稳定物化 Span 树，不依赖 Agent Push。

| 编号 | onto-agent 改动 | 验收 |
|------|-----------------|------|
| L0-01 | 推理 `context` 接收 `agent_run_id`、`langfuse_project`（eval 已注入） | sync 请求体含字段 |
| L0-02 | Agent 只需保证 Langfuse `flush()` 成功；eval `internal_sync` Worker 自动拉取物化 | `trace_sync_status: pending → synced` |
| L0-03 | Langfuse trace 含 **完整** `observations[]`（含 parentObservationId、usage） | PG `run_span_nodes` 行数 > 0 |
| L0-04 | `trace_id` 与 eval 规则一致：`uuid5(NAMESPACE_DNS, "{session_id}:{run_id}").hex` | eval `onto_agent_trace_id` 单测通过 |

**未达标影响**：场景子步骤 / Span 对比 / 过程点 Preview 全部不可用或长期 `pending`。

---

### 3.2 L1 — OTel 契约（P0，与 L0 并行）

**目标**：eval Builder 能生成稳定 `aggregate_key`，过程点 JSONPath 可命中。

| Attribute | 优先级 | 写入时机 |
|-----------|--------|----------|
| `agent.logical_agent_id` | 必填 | 所有业务 observation |
| `agent.version` | 必填 | 所有业务 observation |
| `agent.step.id` | 推荐→必填 | tool / 参与判分的 llm |
| `agent.step.kind` | 推荐 | `tool` / `llm` / `router` |
| `agent.tool.name` | tool 必填 | tool observation |
| `agent.tool.call_index` | tool 必填 | 同 step 多次调用区分 |
| `agent.is_retry` | 可选 | 重试 span |
| `agent.step.id_source` | 推荐 | `registry` / `structural_fallback` |

**aggregate_key 推导规则（eval 侧，agent 须保证 metadata 可读）**：

```text
1. step:{agent.step.id}     ← 优先
2. tool:{agent.tool.name}   ← 次选
3. legacy:{type}:{name}     ← 降级（Diff/对比稳定性差）
```

**验收**：

- [ ] 抽样 trace：≥80% 业务 span 含 `agent.step.id` 或 `agent.tool.name`
- [ ] `legacy_aggregate_report` 中 `legacy:*` 占比 < 20%

---

### 3.3 L2 — 业务步骤注册表（P0，ST-3，2～3 周）

**目标**：场景子步骤 **精确绑定**，弃用 `span_name_patterns` 主路径。

#### 3.3.1 静态契约：`scenario_steps.yaml`

路径（推荐）：

```text
.onto-runtime/workspaces/{workspace_id}/skills/{skill_name}/scenario_steps.yaml
.onto-runtime/workspaces/{workspace_id}/ontologies/{ontology_id}/scenario_steps.yaml
```

Schema 见 [ST-3 §5.1](./ST-3-Agent场景步骤埋点-需求规格.md#51-数据文件scenario_stepsyamlskill-侧契约)。

#### 3.3.2 运行时：StepResolver

在 **创建 Langfuse observation 之前**：

1. 输入：`tool_name`、`tool_input`、`iteration`、`phase`、`ontology_id`
2. 按注册表顺序首条命中 → `agent.step.id = step_id`，`agent.step.id_source = registry`
3. 未命中 → `agent.step.id = tool:{tool_name}` 或 `llm:main:{iteration}`，`id_source = structural_fallback`
4. tool 类写 `agent.tool.call_index`（按 tool_name 递增）

#### 3.3.3 P0 缺陷修复：metadata Sanitize

`LangfuseTraceAdapter._sanitize_dict` 须：

- **`agent.*` 键永不 redact**
- 白名单键名保留：`entity`, `resource`, `object_key`, `operation_key`

否则注册表 match 与 eval 联调无法验证。

**验收**：

- [ ] Langfuse Public API 读回 observation **含** `metadata.agent.step.id`（非仅单元测试）
- [ ] ECO 类场景：`eco.query_affected_objects` 等与 eval `trace_binding.agent_step_id` 精确匹配
- [ ] `registry_sourced / (tool+llm_observations) ≥ 80%`

---

### 3.4 L3 — 质量门禁与可运维 API（P1，1 周）

**目标**：联调可量化、版本升级可回归。

| 能力 | 责任方 |
|------|--------|
| Agent 侧 step-id 覆盖度报告 | onto-agent 新增 API（§4.2） |
| eval 侧绑定试跑 | 已有 `batch-preview-trace` |
| 注册表校验 CI | onto-agent `POST /agentic/scenario-steps/validate` |
| 版本 Diff 回归 | eval `POST /api/comparisons/compare` + structural/semantic |

---

## 4. 外部 API 契约

### 4.1 onto-agent 暴露（供 eval / 运维 / CI）

**Base URL**：`{ONTO_AGENT_BASE_URL}`（默认 `http://127.0.0.1:5055`）  
**鉴权**：与现有 `/agentic/*` 一致（Bearer Token）

#### 4.1.1 查询 Skill 场景步骤注册表

```http
GET /agentic/skills/{skill_name}/scenario-steps?workspace_id={workspace_id}
```

可选 Query：`ontology_id`

**Response 200**

```json
{
  "skill_name": "aviation-plm-knowledge-agent",
  "workspace_id": "eco",
  "registry_id": "aviation-plm-knowledge-agent",
  "registry_version": "1.0.0",
  "schema_version": "1.0",
  "source_path": ".onto-runtime/workspaces/eco/skills/.../scenario_steps.yaml",
  "loaded_at": "2026-06-04T12:00:00Z",
  "steps": [
    {
      "id": "eco.query_affected_objects",
      "label": "查询 ECO 受影响对象",
      "kind": "tool",
      "match": {
        "tool_name": "business_data_search",
        "input": { "entity": "ECO受影响对象" }
      }
    }
  ]
}
```

**Response 404**：Skill 不存在或无注册表。

---

#### 4.1.2 按本体查询注册表

```http
GET /agentic/ontologies/{ontology_id}/scenario-steps?workspace_id={workspace_id}
```

Response 同 4.1.1。

---

#### 4.1.3 校验注册表（CI / Skill 上传）

```http
POST /agentic/scenario-steps/validate
Content-Type: application/json
```

**Request**

```json
{
  "workspace_id": "eco",
  "registry_yaml": "registry_id: eco\nsteps:\n  - id: eco.query_affected_objects\n    kind: tool\n    match:\n      tool_name: business_data_search"
}
```

**Response 200（成功）**

```json
{
  "valid": true,
  "registry_id": "eco",
  "step_count": 6,
  "warnings": []
}
```

**Response 200（失败，HTTP 仍 200）**

```json
{
  "valid": false,
  "errors": [
    { "path": "steps[2].id", "message": "duplicate step id: eco.query_affected_objects" }
  ]
}
```

---

#### 4.1.4 联调探针：Run 步骤 ID 覆盖度

```http
GET /agentic/observability/runs/{run_id}/step-id-report?workspace_id={workspace_id}
```

**Response 200**

```json
{
  "run_id": "agentic-1780543576587-dcbd071a022ba8",
  "trace_id": "7e76a9b8328c55fc9db10d4581dd82a5",
  "observation_count": 12,
  "with_agent_step_id": 10,
  "registry_sourced": 8,
  "structural_fallback": 2,
  "step_ids": [
    {
      "agent_step_id": "eco.query_affected_objects",
      "agent_step.id_source": "registry",
      "observation_name": "tool:business_data_search",
      "tool_call_index": 1,
      "iteration": 3
    }
  ],
  "registry_coverage": {
    "expected_from_registry": 6,
    "hit": 5,
    "missing": ["eco.load_object_schema"]
  }
}
```

**门禁建议**：`registry_sourced / max(tool+llm_observations, 1) ≥ 0.8`

---

#### 4.1.5 已有推理 API（不变，eval 调用）

```http
POST /agentic/runs/sync
Content-Type: application/json
Authorization: Bearer {token}
```

**eval 注入 context（L0 必需）**

```json
{
  "request": "用户问题",
  "context": {
    "agent_mode": "default",
    "agent_run_id": "run_xxxxxxxxxxxx",
    "langfuse_project": "ld_test",
    "onto_eval_task_id": "task_xxx",
    "onto_eval_question_id": "42"
  }
}
```

**Response 须含**（效能统计）：

```json
{
  "answer": "...",
  "run_id": "agentic-...",
  "usage": {
    "total_tokens": 1280,
    "tokens_llm": 1100,
    "tokens_tool": 180
  }
}
```

---

#### 4.1.6 已有系统配置 API（eval Agent 管理自动匹配 Langfuse）

```http
GET /api/system-config
```

eval 读取 `observability.langfuse.public_key` 自动匹配 `langfuse.projects.json`（已实现）。

---

### 4.2 onto-eval 暴露（供联调验证）

**Base URL**：`{ONTO_EVAL_BASE_URL}/api`（推荐带 `/api` 前缀）  
**OpenAPI**：[`openapi/onto-agent-integration.yaml`](./openapi/onto-agent-integration.yaml)

#### 4.2.1 事实摄入（已废弃 Push，主路径为 internal_sync）

> **已废弃（v1.22）**：`POST /api/trace/ingest/push` 端点已废弃，Agent 双写 Push 功能已完全取消。
>
> **当前主路径**：eval `internal_sync` Worker 从 Langfuse API 拉取 trace 物化。Agent 只需保证 Langfuse trace 入库（`flush()` 成功），无需向 eval 推送任何数据。
>
> Agent **无需** 配置 `ONTO_EVAL_BASE_URL` 或 `ONTO_EVAL_PUSH_ENABLED`。

---

#### 4.2.2 场景子步骤 Trace 绑定试跑（ST-3 联调）

```http
POST /api/scenario-eval/items/{dataset_item_id}/steps/batch-preview-trace
Content-Type: application/json
```

**Request**

```json
{
  "agent_run_id": "run_xxx",
  "trace_id": "trace_yyy"
}
```

**Response 200（节选）**

```json
{
  "steps": [
    {
      "step_key": "N1",
      "bind_source": "agent_step_id",
      "trace_binding": {
        "observation_id": "obs_tool_1",
        "agent_step_id": "eco.query_affected_objects"
      },
      "score_preview": 0.67
    }
  ]
}
```

---

#### 4.2.3 Run Span 树（版本对比 UI）

```http
GET /api/runs/{run_id}/span-tree
```

**Response 200（节选）**

```json
{
  "run_id": "run_xxx",
  "root_id": "node_root",
  "nodes": [
    {
      "id": "node_1",
      "aggregate_key": "step:eco.query_affected_objects",
      "semantic_role": "tool",
      "latency_ms": 320,
      "tokens": 0,
      "children": []
    }
  ]
}
```

---

#### 4.2.4 双 Run Span 树对比

```http
GET /api/comparisons/{comparison_id}/span-trees
```

**Response 200（节选）**

```json
{
  "left_run_id": "run_v1",
  "right_run_id": "run_v2",
  "align_aggregate_keys": [
    "step:eco.query_affected_objects",
    "step:eco.summarize_answer"
  ],
  "left_tree": { "nodes": [] },
  "right_tree": { "nodes": [] }
}
```

---

#### 4.2.5 版本 Diff（多版本对比）

```http
POST /api/comparisons/compare
Content-Type: application/json
```

**Request**

```json
{
  "left_run_id": "run_v1",
  "right_run_id": "run_v2",
  "diff_types": [
    "outcome_diff",
    "cost_diff",
    "checkpoint_diff",
    "semantic_diff",
    "structural_diff"
  ]
}
```

**依赖 agent 侧**：稳定 `aggregate_key`（来自 `agent.step.id`）+ 完整 Span 父子边 + usage。

---

#### 4.2.6 eval 辅助：从 Agent 注册表导入子步骤草稿（可选 ST-3b）

```http
GET /api/scenario-eval/import/agent-scenario-steps?agent_name={name}&skill_name={skill}&workspace_id={ws}
```

eval 转发 onto-agent `GET .../scenario-steps`，返回可编辑草稿（人工映射 N1/N2/N3）。

---

## 5. onto-agent 代码改动清单

| 模块 | 文件（建议） | 改动 |
|------|-------------|------|
| Step 注册表 | `agentic/scenario/registry_loader.py` | 加载/校验 `scenario_steps.yaml` |
| Step 解析 | `agentic/scenario/step_resolver.py` | match → `agent.step.id` |
| OTel  enrichment | `agentic/observability/otel_enrich.py` | 统一写 metadata |
| Langfuse 导出 | `agentic/observability/exporters/langfuse/exporter.py` | 调用 enrich；修复 sanitize |
| Sync 钩子 | `agentic/routes/workflows.py` | 确保 Langfuse flush() 成功（无需 Push） |
| HTTP API | `agentic/routes/scenario_steps.py` | §4.1.1～4.1.4 |
| 探针 | `agentic/routes/observability_report.py` | step-id-report |

**eval 侧并行（非 onto-agent，但阻塞联调验收）**：

- 子步骤 `trace_binding.agent_step_id` 配置 UI
- `bind_source=agent_step_id` 优先于启发式
- Langfuse 就绪分级提示（L1/L2 入口）

---

## 6. 里程碑建议

| 阶段 | 周期 | 交付 | 可验证能力 |
|------|------|------|------------|
| **M-A** | 1 周 | L0 + L1 最小集 | internal_sync 物化、过程点 Preview 基本可用 |
| **M-B** | 2 周 | L2 注册表 + StepResolver | 场景子步骤精确绑定 |
| **M-C** | 1 周 | L3 API + 门禁 | 多版本 structural diff、CI 校验 |
| **M-D** | 持续 | 覆盖率治理 | legacy < 20%，registry ≥ 80% |

---

## 7. 风险与决策

| 风险 | 缓解 |
|------|------|
| 只在 Skill 文档写 step.id，运行时未注入 metadata | **强制** exporter 单点 enrichment + step-id-report 门禁 |
| 同 tool 多次调用绑错步 | `agent.tool.call_index` + eval 绑定规则支持 call_index |
| internal_sync Worker 未开启导致 eval 无 Span | 确保 `TRACE_SYNC_WORKER_ENABLED=true`；eval `trace_sync_status` 告警 |
| 版本升级 step.id 漂移 | registry `version` 字段 + 变更评审；Diff 对齐 aggregate_key |
| sanitize 打码导致 match 失效 | P0 白名单修复 |

---

## 8. 变更记录

| 日期 | 版本 | 说明 |
|------|------|------|
| 2026-06-04 | v2.0 | CTO 审视版：分层架构 + 完整外部 API + 明确 step.id 边界 |
