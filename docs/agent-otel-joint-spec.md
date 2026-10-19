# agent.* OTel 属性联合规范（M-03）

> eval：`backend/services/otel_attributes.py`  
> agent：`backend/agentic/observability/agent_attributes.py` + Langfuse exporter  
> 观测文档：[onto-agent-observability.md](./onto-agent-observability.md)

## 1. 目标

跨团队统一 **Langfuse observation.metadata** 中的 `agent.*` 键，使 onto-eval Builder、过程点 JSONPath、Structural/Semantic Diff 不依赖易变的 `observation.name`。

## 2. 必选属性

| 键 | 类型 | 上报方 | 说明 |
|----|------|--------|------|
| `agent.step.id` | string | agent | 跨版本稳定步骤 ID（如 `llm:main:1`、`tool:search`） |
| `agent.step.kind` | string | agent | `llm` / `tool` / `router` |

## 3. 推荐属性

| 键 | 类型 | 说明 |
|----|------|------|
| `agent.tool.name` | string | 工具调用节点 |
| `agent.logical_agent_id` | string | 逻辑 Agent |
| `agent.version` | string | Agent 版本号 |

## 4. 上报位置

- **运行时观测**：Langfuse exporter 在 `agent_loop` / `llm` / `tools` / `tool` span 创建时写入 metadata（M-03 已落地）。
- **internal_sync 物化**：eval Worker 从 Langfuse API 拉取 trace 时，observations 须含相同键（H-01）。

## 5. eval 消费

1. `normalize_observation_attributes` 合并 metadata。
2. `derive_aggregate_key` 优先 `agent.step.id`，其次 `agent.tool.name`。
3. `BUILDER_ALLOW_LEGACY_AGGREGATE_KEY=false` 时禁止 `type:name` 回退。

## 6. 验收

```bash
# agent
cd onto-agent/backend && python -m pytest tests/test_agent_attributes.py -q

# eval legacy 占比（任务级）
curl http://127.0.0.1:45000/api/trace/observability/legacy-aggregate?task_id=<id>
```

目标：`legacy:*` aggregate_key 占比持续下降；新 Run 物化后 `attributes_incomplete=false`。

## 7. 迁移

1. 新步骤：只增 `agent.*`，不改对外 observation `name`。
2. 旧 Run：保留 legacy 规则，UI 标注「规则易碎」。
3. 可选映射：`otel_attribute_mapping.yaml` 将内部键映射到 `agent.*`（规划）。
