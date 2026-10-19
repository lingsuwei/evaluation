# 过程点配置 — 基于 onto-agent 自动识别

> 来源：扫描 `0520/onto-agent` 执行链路与 Langfuse 导出实现（`LangfuseExporter`）。  
> **说明**：`0520/skills` 目录**不存在**；Agent 技能在运行时目录  
> `onto-agent/.onto-runtime/workspaces/<workspace_id>/skills/<skill名>/SKILL.md`，需通过 `POST /agentic/skills/upload` 上传。

---

## 1. Agent 结构摘要

### 1.1 执行主路径

```
POST /agentic/runs/stream
  → Orchestrator
  → AgentLoopEngine（ReAct，多轮 iteration）
       → LLM（可返回 tool_calls）
       → 内置工具 batch 执行
  → Langfuse trace
```

### 1.2 内置工具（固定 6 个，多工具类型核心）

| 工具名 | 类别 | 典型用途 |
|--------|------|----------|
| `ontology_context` | regular | 加载本体 / 上下文 |
| `skill` | regular | 读取 SKILL.md 及附属资源 |
| `business_data_search` | business_data_search | 业务数据检索 |
| `execute_script` | regular | 执行本体脚本 |
| `memory` | memory | 记忆读写 |
| `spawn_sub_agent` | orchestration | 多 Agent 子任务（multi_agent 模式） |

Skill **目录**里的技能不会自动执行；LLM 需显式调用工具 **`skill`**，并传 `skill_name`。

### 1.3 Langfuse 当前 span 命名（过程点匹配依据）

| 阶段 | observation `name` | metadata 常用键 |
|------|-------------------|-----------------|
| Run | `agent_request:{run_id}` | run 级统计 |
| Agent 循环 | `agent_loop:main` / `agent_loop:subagent` | `caller`, `iteration` |
| LLM | `llm:main_iteration` / `llm:subagent_iteration` | `caller`, `iteration`, `model` |
| 工具批 | `tools:main_iteration_{n}` | `caller`, `iteration` |
| 单工具 | **`tool:{tool_name}`** | **`tool_name`**, `tool_call_id`, `iteration` |

> **现状**：onto-agent **尚未**写入 `agent.step.id` / `agent.tool.name` 契约字段（见 `onto-agent-observability.md`）。  
> 下列过程点基于 **`observation_name` + `metadata.tool_name`**，与当前 trace **可直接匹配**。

---

## 2. 推荐过程点清单（onto-agent 默认识别）

将 `agent_name` 换成 **onto-eval Agent 管理**里显示的 Agent 名称。

| 顺序 | 过程点 | 必达 | 识别依据 |
|------|--------|------|----------|
| 1 | Run 已启动 | ✅ | trace 含 `agent_request:` |
| 2 | 主 Agent 循环 | ✅ | `agent_loop:main` |
| 3 | LLM 推理轮次 | ✅ | `llm:main_iteration` |
| 4 | 发生过工具调用 | ✅ | 任意 `tool:*` 或 metadata.tool_name |
| 5 | 本体上下文加载 | ✅ | `tool:ontology_context` |
| 6 | Skill 文档加载 | ❌ | `tool:skill`（有上传 skill 时常见） |
| 7 | 业务数据检索 | ❌ | `tool:business_data_search` |
| 8 | 本体脚本执行 | ❌ | `tool:execute_script` |
| 9 | 记忆操作 | ❌ | `tool:memory` |
| 10 | 子 Agent 编排 | ❌ | `tool:spawn_sub_agent`（multi_agent） |

---

## 3. match_rules（可直接粘贴）

### CP1 Run 已启动（order=1）

```json
{
  "mode": "any",
  "rules": [
    { "type": "observation_name", "op": "contains", "value": "agent_request:" }
  ]
}
```

### CP2 主 Agent 循环（order=2）

```json
{
  "mode": "any",
  "rules": [
    { "type": "observation_name", "op": "eq", "value": "agent_loop:main" }
  ]
}
```

### CP3 LLM 推理（order=3）

```json
{
  "mode": "any",
  "rules": [
    { "type": "observation_name", "op": "contains", "value": "llm:main_iteration" }
  ]
}
```

### CP4 任意工具调用（order=4）

```json
{
  "mode": "any",
  "rules": [
    { "type": "observation_name", "op": "contains", "value": "tool:" },
    {
      "type": "metadata",
      "key": "tool_name",
      "op": "regex",
      "value": ".+"
    }
  ]
}
```

### CP5 本体上下文（order=5，必达）

```json
{
  "mode": "any",
  "rules": [
    { "type": "observation_name", "op": "eq", "value": "tool:ontology_context" },
    { "type": "metadata", "key": "tool_name", "op": "eq", "value": "ontology_context" }
  ]
}
```

### CP6 Skill 加载（order=6，非必达）

```json
{
  "mode": "any",
  "rules": [
    { "type": "observation_name", "op": "eq", "value": "tool:skill" },
    { "type": "metadata", "key": "tool_name", "op": "eq", "value": "skill" }
  ]
}
```

### CP7 业务数据检索（非必达）

```json
{
  "mode": "any",
  "rules": [
    { "type": "metadata", "key": "tool_name", "op": "eq", "value": "business_data_search" }
  ]
}
```

### CP8 脚本执行（非必达）

```json
{
  "mode": "any",
  "rules": [
    { "type": "metadata", "key": "tool_name", "op": "eq", "value": "execute_script" }
  ]
}
```

### CP9 记忆（非必达）

```json
{
  "mode": "any",
  "rules": [
    { "type": "metadata", "key": "tool_name", "op": "eq", "value": "memory" }
  ]
}
```

### CP10 子 Agent（非必达）

```json
{
  "mode": "any",
  "rules": [
    { "type": "metadata", "key": "tool_name", "op": "eq", "value": "spawn_sub_agent" },
    { "type": "observation_name", "op": "eq", "value": "agent_loop:subagent" }
  ]
}
```

---

## 4. 批量导入 JSON

见同目录 [`M2-过程点配置-onto-agent识别.json`](./M2-过程点配置-onto-agent识别.json)。  
使用前将文件中 `"agent_name": "YOUR_AGENT_NAME"` 改为实际 Agent 名称，再逐条 `POST /api/checkpoints/templates`。

### 4.1 扫描本机已上传 Skill（推荐）

`.onto-runtime` 在 **onto-agent 仓库根目录**，且被 `.gitignore` 忽略；**首次启动 onto-agent / docker-compose 后**才会创建。

```powershell
cd d:\work\全链路工具优化\soft\0520\onto-agent

# 需 onto-agent 已运行且已 upload skill
python scripts/list_skills_checkpoints.py --agent-name "你的Agent名称" `
  --json-out ..\onto-eval\docs\M2-过程点-skill扫描结果.json
```

脚本会：

1. 扫描 `.onto-runtime/workspaces/*/skills/**/SKILL.md` 与 `system/skills/**`
2. 打印 skill 清单（name、description、workspace）
3. 在 5 条必达基线后，为**每个 skill** 追加一条「Skill 加载：{name}」过程点建议（M2 仅能匹配 `tool:skill`）

**当前本机扫描结果（2026-05-24）**：`.onto-runtime` **不存在**，skill 数量 **0**（onto-agent 未在本机运行或未 upload）。

PowerShell 示例：

```powershell
$base = "http://127.0.0.1:8000/api/checkpoints/templates"
$templates = Get-Content ".\docs\M2-过程点配置-onto-agent识别.json" | ConvertFrom-Json
foreach ($t in $templates.templates) {
  Invoke-RestMethod -Method Post -Uri $base -ContentType "application/json" -Body ($t | ConvertTo-Json -Depth 10)
}
```

---

## 5. 与「skills 目录」的关系

| 问题 | 答案 |
|------|------|
| `0520/skills` 有内容吗？ | **无**（目录不存在或未纳入仓库） |
| Skill 存在哪？ | `onto-agent/.onto-runtime/workspaces/<workspace>/skills/<name>/SKILL.md` |
| 过程点如何覆盖 skill？ | 匹配 **`tool:skill` 是否被调用**；具体 `skill_name` 在 tool **input** 中，M2 规则引擎暂不解析 input |
| 如何为某个 skill 单独设过程点？ | 需 onto-agent 在 metadata 写入 `skill_name`，或升级 M3+ 规则 |

---

## 6. 后续升级（契约埋点后）

onto-agent 在 `LangfuseExporter` 写入以下字段后，可将 CP5～CP10 升级为 jsonpath：

```python
metadata={
    "agent.step.kind": "tool",
    "agent.tool.name": tool_name,
    "agent.step.id": f"tool_{tool_name}",
    "agent.parent.step.id": "agent_loop",
}
```

详见 [onto-agent-observability.md](./onto-agent-observability.md)。

---

## 相关文档

- [M2-过程点配置样例.md](./M2-过程点配置样例.md) — 通用 / 多工具抽象样例
- [M2-手工验收清单.md](./M2-手工验收清单.md)
- `onto-agent/backend/agentic/tools/registry.py` — 工具注册表
- `onto-agent/backend/agentic/observability/exporters/langfuse/exporter.py` — Langfuse span 命名
