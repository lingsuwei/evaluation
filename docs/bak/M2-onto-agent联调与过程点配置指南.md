# onto-agent × onto-eval 联调与过程点配置用户指南

> **适用读者**：需要在本地/测试环境部署 onto-agent，并在 onto-eval 中配置过程点、跑在线评测的用户。  
> **更新日期**：2026-05-24

---

## 1. 整体关系

```
用户 → onto-eval（评测编排、过程点配置、Trace 探索）
         ↓ 调用推理
       onto-agent backend（:5055，ReAct + 多工具）
         ↓ Langfuse SDK
       Langfuse（Trace 存储）
         ↑ BFF 读取
       onto-eval 过程点 Preview / Trace 探索
```

| 组件 | 本地开发（npm / uvicorn） | Docker Compose（`onto-eval-app`） |
|------|---------------------------|-----------------------------------|
| onto-eval **前端 UI** | `http://localhost:3000`（`npm run dev`） | **`http://localhost:45000`**（nginx → 静态页） |
| onto-eval **后端 API** | `http://127.0.0.1:8000` | **`http://127.0.0.1:3000`**（映射容器内 8000） |
| API 经 nginx 反代 | — | `http://localhost:45000/api/...` |
| Swagger `/docs` | `http://127.0.0.1:8000/docs` | **`http://127.0.0.1:3000/docs`**（不是前端页） |
| onto-agent backend | — | `http://127.0.0.1:5055` |
| onto-agent frontend | — | 默认 **`http://127.0.0.1:8888`**（与 onto-eval **无关**） |

> **常见误区**：容器模式下访问 `localhost:3000` 打开的是 **FastAPI 后端**，根路径 `/` 会返回 `{"detail":"Not Found"}`，**不是**前端页面。  
> **8888** 是 **onto-agent 前端**，其 `/docs` 多为 SPA 回退 HTML，**不是** onto-eval 的 Swagger。

**说明**：过程点配置在 **onto-eval**；Agent 执行与 Skill 文件在 **onto-agent**。

---

## 2. 启动 onto-agent（Docker）

### 2.1 启动命令

```bash
cd /path/to/0520/onto-agent
docker compose up -d
docker ps
```

### 2.2 期望状态

| 容器 | 期望 STATUS | 说明 |
|------|-------------|------|
| `onto-agent-backend-latest` | `Up (healthy)` | 必须正常 |
| `onto-agent-frontend-latest` | `Up` | 可选；仅 Web 上传 skill 时需要 |

验证 backend：

```bash
curl -s http://127.0.0.1:5055/health
# 期望：{"status":"ok"}
```

### 2.3 Frontend 反复重启（exit 127）

**现象**：`docker ps` 中 frontend 为 `Restarting (127)`，日志类似：

```text
/docker-entrypoint.d/99-runtime-env.sh: not found
```

**原因**：`frontend/docker-entrypoint.d/99-runtime-env.sh` 在 Windows 下常为 **CRLF 行尾**，Linux 容器执行 shebang 失败。

**修复**（在 WSL 或 Linux 下执行一次）：

```bash
sed -i 's/\r$//' frontend/docker-entrypoint.d/99-runtime-env.sh
docker compose build frontend --no-cache
docker compose up -d frontend
docker ps   # frontend 应为 Up，不再 Restarting
```

> 若暂不使用 onto-agent Web 界面，可 **只保留 backend** 联调评测；Skill 可通过 API 或手工目录上传（见 §4）。

---

## 3. `.onto-runtime` 与 Skill 目录

### 3.1 位置

Skill **不在** 仓库根目录 `0520/skills`，而在：

```text
onto-agent/.onto-runtime/
├── workspaces/
│   └── <workspace_id>/
│       └── skills/
│           └── <skill名>/
│               └── SKILL.md
└── system/
    └── skills/
        └── <skill名>/
            └── SKILL.md
```

- 目录被 **`.gitignore` 忽略**，首次启动容器后才会在宿主机出现。
- Docker 挂载：`./.onto-runtime:/app/.onto-runtime`

### 3.2 空目录是正常的

刚启动时可能只有：

```text
.onto-runtime/memory/
.onto-runtime/operations-evolution/
```

表示 **尚未创建 workspace / 尚未上传 skill**。上传或首次使用工作区后会出现 `workspaces/.../skills/`。

### 3.3 扫描本机 Skill

```bash
cd /path/to/onto-agent
python3 scripts/list_skills_checkpoints.py \
  --agent-name "你在onto-eval里注册的Agent名称" \
  --json-out ../onto-eval/docs/M2-过程点-skill扫描结果.json
```

输出：

- 终端打印 skill 清单（name、description、workspace）
- JSON 含 **5 条必达基线** + 每个 skill 一条建议过程点

---

## 4. 上传 Skill（三选一）

### 4.1 Web 上传（需 frontend 正常）

1. 浏览器打开 onto-agent 前端  
2. 使用 Skill 管理上传 zip/tar  
3. 包内结构：`<skill名>/SKILL.md`（YAML frontmatter 含 `name`、`description`）

### 4.2 API 上传（仅需 backend）

```bash
curl -X POST http://127.0.0.1:5055/agentic/skills/upload \
  -H "Authorization: Bearer <你的token>" \
  -F "file=@your-skill-pack.zip"
```

Token 获取方式见 `onto-agent/docs/api.md` 认证章节。

### 4.3 手工放置

```bash
mkdir -p .onto-runtime/workspaces/default/skills/my-skill
# 编辑 .onto-runtime/workspaces/default/skills/my-skill/SKILL.md
```

`SKILL.md` 示例：

```markdown
---
name: my-skill
description: 示例可复用技能
---

# My Skill
正文...
```

上传后重新执行 §3.3 扫描脚本。

---

## 5. 过程点配置（onto-eval）

### 5.1 入口

**资源管理 → 过程点配置**（或 **系统设置** 下，以实际菜单为准）

- 按 **Agent 名称** 筛选（不是 logical_agent_id）
- 新增模板、填写 `match_rules` JSON、**Preview** 试跑

### 5.2 onto-agent 默认识别（推荐）

onto-agent 为 **多工具 ReAct Agent**，内置 6 个工具：

| 工具名 | 用途 |
|--------|------|
| `ontology_context` | 本体上下文 |
| `skill` | 加载 SKILL.md |
| `business_data_search` | 业务数据检索 |
| `execute_script` | 执行脚本 |
| `memory` | 记忆 |
| `spawn_sub_agent` | 子 Agent |

Langfuse span 命名（当前可用于 M2 规则）：

| 阶段 | observation 名称 |
|------|------------------|
| Run | `agent_request:{run_id}` |
| 主循环 | `agent_loop:main` |
| LLM | `llm:main_iteration` |
| 工具 | `tool:{tool_name}`，metadata 含 `tool_name` |

**批量 JSON（改 agent_name 后导入）**：

- [M2-过程点配置-onto-agent识别.json](./M2-过程点配置-onto-agent识别.json)
- 说明：[M2-过程点配置-onto-agent识别.md](./M2-过程点配置-onto-agent识别.md)

**建议必达 5 条**：Run 已启动 → 主 Agent 循环 → LLM 推理 → 发生过工具调用 → 本体上下文加载。

### 5.3 扫描结果 JSON

上传 skill 后运行 §3.3 脚本，生成：

- [M2-过程点-skill扫描结果.json](./M2-过程点-skill扫描结果.json)（运行后覆盖更新）

### 5.4 批量导入（推荐）

确保 **Agent 已在 onto-eval 注册**，且 onto-eval backend 已启动，然后：

```bash
cd /path/to/onto-eval/backend
python3 scripts/import_checkpoint_templates.py \
  --json ../docs/M2-过程点配置-onto-agent识别.json \
  --agent-name "你在Agent管理里注册的名称"
```

预览（不写库）：

```bash
python3 scripts/import_checkpoint_templates.py \
  --json ../docs/M2-过程点配置-onto-agent识别.json \
  --agent-name "你的Agent名称" \
  --dry-run
```

Skill 扫描 JSON 同样可导入：

```bash
python3 scripts/import_checkpoint_templates.py \
  --json ../docs/M2-过程点-skill扫描结果.json \
  --agent-name "你的Agent名称"
```

默认 **跳过同名** 模板；重复执行安全。

### 5.5 导入单条模板（API）

```bash
curl -X POST http://127.0.0.1:8000/api/checkpoints/templates \
  -H "Content-Type: application/json" \
  -d '{
    "agent_name": "你的Agent名称",
    "name": "本体上下文加载",
    "order": 5,
    "required": true,
    "match_rules": {
      "mode": "any",
      "rules": [
        { "type": "observation_name", "op": "eq", "value": "tool:ontology_context" }
      ]
    }
  }'
```

### 5.6 Preview 试跑

页面填写 Langfuse **trace_id**，或：

```bash
curl -X POST "http://127.0.0.1:8000/api/checkpoints/templates/{模板ID}/preview" \
  -H "Content-Type: application/json" \
  -d '{"trace_id": "你的trace_id", "project": "langfuse项目id"}'
```

### 5.7 限制（M2）

| 能力 | 支持 |
|------|------|
| 匹配 `tool:skill` 是否调用 | ✅ |
| 按 **skill_name** 区分具体 skill | ❌（skill_name 在 tool input 中，M2 不解析） |
| `agent.step.*` jsonpath | ⏳ 需 onto-agent exporter 埋点后升级规则 |

---

## 6. 与 onto-eval 联调最小路径

按顺序完成即可打通「评测 → Trace → 过程点 Preview」：

| 步骤 | 操作 | 通过标准 |
|------|------|----------|
| 1 | 启动 onto-eval（Docker 或 dev） | **UI：`http://localhost:45000`**（Docker）或 `http://localhost:3000`（npm run dev） |
| 2 | 启动 onto-agent backend（5055） | `curl .../health` → ok |
| 3 | 配置 Langfuse（onto-eval `.env` + `langfuse.projects.json`） | `/api/trace/health` 正常 |
| 4 | **Agent 管理**：注册 Agent，推理 URL 指向 onto-agent | 例：`http://127.0.0.1:5055`，endpoint `/agentic/runs/sync` |
| 5 | **过程点配置**：§5.4 批量导入 JSON | 过程点列表 ≥5 条 |
| 6 | **在线评测**：跑 1 题 | Langfuse 出现 trace |
| 7 | **过程点 Preview**：填 trace_id | 返回 hit / 未命中 |

### 6.1 Agent 推理配置示例

在 Agent 管理 → 推理配置中（字段名以 UI 为准）：

| 项 | 示例值 |
|----|--------|
| URL | `http://127.0.0.1:5055` |
| Endpoint | `/agentic/runs/sync` 或 `/agentic/runs/stream`（与你们接入方式一致） |
| Langfuse 项目 | 与 Agent 绑定的项目空间一致 |

### 6.2 手工验收

完整检查项见：[M2-手工验收清单.md](./M2-手工验收清单.md)

---

## 7. 常见问题

| 现象 | 原因 | 处理 |
|------|------|------|
| `localhost:3000` 显示 `{"detail":"Not Found"}` | Docker 下 3000 是 **后端 API**，不是前端 | 前端请开 **`http://localhost:45000`** |
| 把 `8888/docs` 当成 onto-eval | **8888 是 onto-agent 前端** | onto-eval Swagger：`http://127.0.0.1:3000/docs`（Docker）或 `8000/docs`（本地 uvicorn） |
| `.onto-runtime` 不存在 | 未启动过 onto-agent | `docker compose up -d` |
| skill 数量为 0 | 未 upload / 无 workspace | §4 上传后重新扫描 |
| frontend Restarting (127) | entrypoint 脚本 CRLF | §2.3 修复并 rebuild |
| Preview 401 | Langfuse 不可用或 Key 错误 | 检查 Langfuse 与 `langfuse.projects.json` |
| 过程点全未命中 | trace 来自旧 Agent 或非 onto-agent | 确认评测走 5055 且 exporter 已开 Langfuse |
| `0520/skills` 找不到 | 该路径不存在 | 使用 `onto-agent/.onto-runtime/.../skills` |

---

## 8. 相关文档索引

| 文档 | 内容 |
|------|------|
| [M2-过程点配置.md](./M2-过程点配置.md) | M2 交付范围与 API |
| [M2-过程点配置样例.md](./M2-过程点配置样例.md) | RAG / 多工具通用样例 |
| [M2-过程点配置-onto-agent识别.md](./M2-过程点配置-onto-agent识别.md) | 基于 onto-agent 代码识别 |
| [M2-手工验收清单.md](./M2-手工验收清单.md) | QA 逐步验收 |
| [onto-agent/docs/api.md](../onto-agent/docs/api.md) | Skill / Runs API |
| [onto-agent-observability.md](./onto-agent-observability.md) | `agent.*` 埋点契约（目标态） |

---

## 9. 快速命令备忘

```bash
# 状态
docker ps
curl -s http://127.0.0.1:5055/health
curl -s "http://127.0.0.1:8000/api/trace/health?project=你的项目"

# 修 frontend + 重建
cd onto-agent
sed -i 's/\r$//' frontend/docker-entrypoint.d/99-runtime-env.sh
docker compose build frontend --no-cache && docker compose up -d frontend

# 批量导入过程点（在 onto-eval/backend 下；需 backend 8000 + 已注册 Agent）
python3 scripts/import_checkpoint_templates.py \
  --json ../docs/M2-过程点配置-onto-agent识别.json \
  --agent-name "你的Agent名称"

# 扫描 onto-agent skill → 更新过程点 JSON（在 onto-agent 下）
python3 scripts/list_skills_checkpoints.py \
  --agent-name "你的Agent名称" \
  --json-out ../onto-eval/docs/M2-过程点-skill扫描结果.json
```
