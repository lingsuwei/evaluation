# onto-agent 可观测性契约（OTel Attributes）

> 与 [架构设计说明书 §8.8](./架构设计说明书.md) 对齐。onto-eval 过程点、Structural 聚合以本契约为准，**不以** Langfuse `observation.name` 为跨版本强绑定。

## 1. 原则

- 语义写在 **Attributes**（键值字典），显示名 `name` 可随实现重构。
- 键名使用 `agent.` 前缀，避免与 Langfuse / OTel 保留字段冲突。
- 值须可 JSON 序列化：`string` | `int` | `bool`（浮点尽量少用，必要时转 string）。

## 2. 必填与推荐

| 优先级 | Attribute | 类型 | 说明 |
|--------|-----------|------|------|
| **推荐** | `agent.step.id` | string | 稳定步骤 ID，跨版本不变（如 `planner`, `retrieve`, `answer`） |
| **推荐** | `agent.step.kind` | string | `tool` / `llm` / `router` 等 |
| 工具步骤 | `agent.tool.name` | string | 逻辑工具名，非 Python 函数名 |
| 重试 | `agent.is_retry` | bool | 是否重试 |
| 已有 | `agent.logical_agent_id` | string | 与评测域 Agent 对齐 |
| 已有 | `agent.version` | string | Agent 版本 |
| L1 回放 | `agent.is_deterministic_io` | bool | 确定性 IO，可 mock 回放（P5-E） |
| L1 回放 | `agent.mock_response_payload` | object | mock 输出 JSON，Builder 写入 span |

可选：`agent.tool.call_index`（int）、`agent.parent.step.id`（string，用于并行子步骤）。

## 3. 上报方式（示意）

在 Langfuse observation 的 **metadata** 中写入上述键（onto-eval Builder 会归一化到 `run_span_nodes.attributes`）：

```python
langfuse.span(
    name="internal_fn_name",  # 可随意，不作为平台契约
    metadata={
        "agent.step.id": "retrieve",
        "agent.tool.name": "kb_search",
        "agent.is_retry": False,
    },
)
```

SDK 层建议封装 `set_agent_attribute(key, value)`，避免业务散落字符串。

## 4. 平台侧消费

| 能力 | 匹配方式 |
|------|----------|
| 过程点 M2 | `observation_name` / `metadata` 单键（兼容） |
| 过程点 v2 | `match_rules` 中 `type: jsonpath`，路径如 `$.attributes.agent.step.id` |
| Structural 聚合 | `aggregate_key` 优先 `agent.step.id` / `agent.tool.name` |
| 确定性 IO 元数据 | `is_deterministic_io` + `mock_response_payload` 落库（观测用途，无 replay 消费） |

未上报推荐属性时：评测主路径不阻塞；过程点 / Diff 走降级（name 匹配），UI 标注规则易碎。

## 6. sync `usage` 与 onto-eval 物化（H-01 / H-05）

`/agentic/runs/sync` 与 `turn.finished` 事件可携带 **RunUsageLedger** 汇总字段 `usage`：

| 字段 | 说明 |
|------|------|
| `total_tokens` | 总 Token |
| `tokens_llm` | LLM 路径 Token |
| `tokens_tool` | 工具路径 Token；无实测时 **omit**（eval G-04） |

Agent 推理结束后只需保证 Langfuse `flush()` 成功；eval 侧通过 `internal_sync` Worker 从 Langfuse API 拉取 trace 物化，**无需 Agent 向 eval 推送任何数据**。

> **已废弃（v1.22）**：`POST /api/trace/ingest/push` 端点已废弃，Agent 双写 Push 功能已取消。Agent 无需配置 `ONTO_EVAL_BASE_URL`。

Langfuse `trace_id` 与 eval 对齐规则：`uuid5(NAMESPACE_DNS, "{session_id}:{run_id}").hex`。

## 7. 迁移

1. 新步骤：只加 Attributes，不改对外 `name` 承诺。
2. 旧模板：保留 M2 规则，逐步改为 JSONPath。
3. 遗留内部键可通过 `otel_attribute_mapping` 映射到 `agent.*`（Builder 配置，规划）。
