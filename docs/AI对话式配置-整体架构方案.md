# onto-eval AI 对话式配置 — 整体架构方案

> **版本**：v1.0
> **日期**：2026-08-14
> **状态**：架构设计（阶段 1 待实施）
> **关联**：阶段 1 细化见 [AI对话式配置-阶段1技术方案.md](./AI对话式配置-阶段1技术方案.md)

---

## 1. 背景与目标

### 1.1 痛点

onto-eval 跑一次评测需走 **7 步流程、跨 6 个页面、填 15+ 个表单字段**（提交 payload 见 `frontend/src/components/EvalPage.tsx:697-751`），还要理解"四维 verdict / gate 封顶 / 场景子步骤 eval_mode"等术语。手工配置门槛高，用户期望"AI 智能化"。

### 1.2 目标

做一个 AI 对话框，用户用自然语言说意图（如"评估 agent X 在数据集 Y 上的忠实度和正确性，跑 3 轮"），由 LLM 完成配置并启动评测。

### 1.3 可行性结论：高

onto-eval 是 LLM 评测平台，AI 基础设施全部现成：

| 现有资产 | 复用方式 | 位置 |
|---|---|---|
| LLM 调用客户端（并发/重试/限流齐全） | 作为对话引擎 | `backend/evaluators/llm_client.py` |
| LLMModel 表 + 管理界面 | 复用配置好的模型当对话框引擎 | `backend/database.py:655-670` |
| 后端配置 API（RESTful） | 包一层成"工具"给 LLM 调用 | `backend/main.py` |
| MCP server 骨架 + backend_client | 复用 HTTP 调用模式 | `mcp/mcp_server.py`、`mcp/backend_client.py` |
| 前端 SSE 流式（评估思维链） | 复用于对话逐字输出 | `backend/tasks.py:16-34` |
| EvaluateRequest schema | 作为对话最终产物结构 | `backend/schemas.py:183-216` |

### 1.4 必须修正的预期

| 误以为现成 | 实际 |
|---|---|
| MCP 已封装配置工具 | 只有 2 个评测工具（`evaluate_answer`/`list_metrics`），无配置工具 |
| 前端有聊天组件 | 没有，需从零建对话 UI |
| 有 agent loop | 没有，需新写"对话编排"逻辑 |

---

## 2. 设计原则

1. **同进程内嵌，不新建独立服务**。SSE 是进程内 `asyncio.Queue` 字典（`tasks.py:21` 明确警告跨进程丢事件），对话编排必须与评估主链路同进程，才能复用 `emit_event` / `get_llm_client` / `async_session`。
2. **工具函数与 MCP 暴露同源**。配置类工具写成纯 `async` 函数，对话编排器直接 `await`，MCP server 套一层 thin wrapper 暴露给外部 IDE —— 一次实现，两处可用，反哺现有 MCP。
3. **架构一次设计，三阶段填充**。阶段 1 是阶段 2 的子集（单轮 LLM、无 tool-calling 循环），阶段 2 加 agent loop，阶段 3 加生成类工具。不重写，只追加。
4. **AI 产出可回填、可人工修改**。对话框不替换 `EvalPage` 表单，而是产出 `EvaluateRequest` 草稿 → 回填表单 → 用户确认后走现有 `POST /evaluate`。失败可降级。
5. **遵守大数据量准则**（CLAUDE.md）。对话会话表分页、LLM 调用走现有 8 并发信号量（`llm_client.py:65`）、对话编排独立 asyncio task 不阻塞评估主链路。

---

## 3. 总体分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│  前端 (React, App.tsx TabType)                                   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │ EvalPage     │◄─│ ChatPage(新) │  │ 配置预览卡(新)        │  │
│  │ (现有表单)   │  │ 对话UI(新)   │──►│ 回填 EvaluateRequest │  │
│  └──────────────┘  └──────────────┘  └──────────────────────┘  │
│         ▲                  ▲                                  │
│         │ POST /evaluate    │ SSE /chat/sessions/{id}/stream   │
│         │ (现有)            │ POST /chat/sessions              │
└─────────┼──────────────────┼──────────────────────────────────┘
          │                  │
┌─────────┼──────────────────┼──────────────────────────────────┐
│  后端   │  FastAPI (main.py) │                                  │
│  ┌──────┴──────────────────┴──────────────────────────────┐   │
│  │ chat/ (新模块)                                          │   │
│  │   orchestrator.py  agent loop: LLM→tool_call→回填→继续 │   │
│  │   tools.py         配置类工具(async 函数集)            │   │
│  │   recommend.py     意图解析+推荐引擎                    │   │
│  │   session.py       会话CRUD + 上下文管理               │   │
│  │   llm_router.py    chat_completion_with_tools           │   │
│  └───────────┬─────────────────┬───────────────────────────┘   │
│              │                 │                              │
│   ┌──────────▼─────┐   ┌──────▼────────────────────┐         │
│   │ evaluators/    │   │ 现有 API (main.py)         │         │
│   │ llm_client.py  │   │ /datasets /agents /llm-models│        │
│   │ (复用 AsyncOpenAI)│ │ /custom-metrics /scenario-eval│     │
│   └────────────────┘   │ /evaluate /tasks            │        │
│                        └────────────────────────────┘         │
│                                                              │
│   ┌────────────────────────────────────────────────────────┐  │
│   │ database.py (PG) + 新增 ChatSession/ChatMessage 表     │  │
│   └────────────────────────────────────────────────────────┘  │
│                                                              │
│   ┌────────────────────────────────────────────────────────┐  │
│   │ mcp/mcp_server.py (套 wrapper 暴露同一批 tools)        │  │
│   └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

---

## 4. 各层组件清单

### 4.1 后端新增模块 `backend/chat/`

| 文件 | 职责 | 复用现有 | 阶段 |
|---|---|---|---|
| `session.py` | ChatSession/ChatMessage CRUD，上下文窗口管理 | `async_session`、`database.py` 模式 | 1 |
| `recommend.py` | 单轮意图解析：自然语言 → `EvaluateRequest` 草稿 + 推荐指标/轮次/阈值 | `registry.py` 指标清单、`METRICS_CONFIG` | 1 |
| `llm_router.py` | `chat_completion_with_tools(messages, tools, model_key)` —— 基于 `AsyncOpenAI` 原生 tool-calling | `get_llm_client`（`llm_client.py:84`）、`LLMModel` 表 | 1（无 tools 版）→ 2（带 tools） |
| `tools.py` | 配置类工具集（见 4.2）—— 纯 async 函数，内部调现有 API | `backend_client.py` 的 `_request` 模式（同进程内可直接走函数调用，免 HTTP） | 2 |
| `orchestrator.py` | agent loop：消息→`llm_router`→解析 tool_calls→执行 `tools.py`→回填→继续，SSE 推送中间态 | `emit_event`/`task_streams` 模式 | 2 |

### 4.2 配置类工具集

工具写成 `async def tool_xxx(args) -> dict`，对接点见下。同一批函数既被 `orchestrator.py` 直接 `await`，又被 `mcp_server.py` 套一层暴露。

| 工具名 | 对接现有 API / 函数 | 阶段 |
|---|---|---|
| `list_datasets` | `GET /datasets` | 2 |
| `list_agents` | `GET /agents`（仅 active） | 2 |
| `list_llm_models` | `GET /llm-models` | 2 |
| `list_metrics` | 复用 `mcp_server.py` 现有 `list_metrics` | 2 |
| `recommend_metrics` | `recommend.py`：按意图（在线/离线、评什么）返回指标组合 | 2 |
| `get_dataset_preview` | `GET /datasets/{id}` 返回字段/条数/场景键 | 2 |
| `create_eval_task` | 映射为 `EvaluateRequest` → 调 `POST /evaluate` 的同进程等价路径 | 2 |
| `build_eval_request` | 组装草稿，返回给前端预览（不启动） | 2 |
| `generate_dataset_from_description` | LLM 生成 question/ground_truth/context | 3 |
| `generate_scenario_template` | LLM 生成 `ScenarioEvalStep[]`，对接 `scenario_eval_service.py` | 3 |
| `recommend_verdict_thresholds` | 按场景推荐四维阈值，对接 `VerdictThresholdEditor` 字段 | 3 |
| `tune_judge_prompt` | 优化自定义指标 G-Eval criteria | 3 |

### 4.3 数据模型新增（`database.py`，走 alembic 迁移）

```
ChatSession
  id (PK), title, status (draft/ready/consumed),
  intent (online/offline), model_key (对话引擎用哪个 LLMModel),
  draft_request (JSON: EvaluateRequest 草稿),
  created_at, updated_at

ChatMessage
  id (PK), session_id (FK), role (user/assistant/tool),
  content (Text), tool_name, tool_args (JSON), tool_result (JSON),
  tokens (int), created_at
```

- `draft_request` 存 `EvaluateRequest` 草稿 JSON，阶段 1 直接落库 → 阶段 2 由 agent loop 增量填充。
- 大数据量约束：`ChatMessage` 按 `session_id` 索引；会话列表分页（复用 `/tasks` 分页模式）；历史会话定期归档/清理任务。

### 4.4 后端 API 新增（`main.py` 追加路由）

| 路由 | 方法 | 说明 | 阶段 |
|---|---|---|---|
| `/chat/sessions` | POST | 建会话，入参含初始意图文本 | 1 |
| `/chat/sessions` | GET | 分页列表 | 1 |
| `/chat/sessions/{id}` | GET | 详情（含消息） | 1 |
| `/chat/sessions/{id}/messages` | POST | 追加用户消息 | 1 |
| `/chat/sessions/{id}/interpret` | POST | **阶段1核心**：单轮意图解析 → 返回 `EvaluateRequest` 草稿 | 1 |
| `/chat/sessions/{id}/stream` | GET | SSE 流式对话（agent loop 中间态） | 2 |
| `/chat/sessions/{id}/draft` | PATCH | 用户在前端改了草稿字段后回写 | 1 |
| `/chat/sessions/{id}/apply` | POST | 草稿 → `POST /evaluate`（同进程函数调用，免二次 HTTP） | 1 |
| `/chat/sessions/{id}` | DELETE | 删会话 | 1 |

### 4.5 前端新增

| 组件 | 文件 | 复用 | 阶段 |
|---|---|---|---|
| `ChatPage` | `frontend/src/components/ChatPage.tsx` | `App.tsx` 新增 `chat` TabType，挂入"评估功能"分组 | 1（含表单回填）→ 2（流式） |
| `ChatMessageList` | 同上目录 | 卡片样式遵循 `onto-eval-ucd` skill | 1 |
| `ChatInput` | 同上 | `ComboDropdown` 风格 | 1 |
| `EvalDraftPreviewCard` | 同上 | 把 `EvaluateRequest` 草稿渲染成可读卡片 + "回填到表单"按钮 | 1 |
| `ToolCallBubble` | 同上 | 工具调用可视化（参数+结果折叠） | 2 |
| `StreamingText` | 同上 | 复用评估页 SSE 接收逻辑（`TaskMonitor.tsx` 模式） | 2 |

**回填机制**：`ChatPage` 与 `EvalPage` 不共享 state（避免耦合），通过 `App.tsx` 的 `singletonPage` 切换 + 全局 `draftRequest` ref/URL query 传递。用户点"回填到表单"→ 跳转 `EvalPage` 并预填。

### 4.6 MCP 反哺

`mcp/mcp_server.py` 的 `call_tool` 分派器（`mcp_server.py:72`）追加配置类工具，每个调用 `chat/tools.py` 的同源函数（经 `backend_client.py` 走 HTTP，因为 MCP 是独立进程）。**外部 IDE 也能用自然语言配置评测任务**，一套工具两处用。

---

## 5. 三阶段递进路线

### 阶段 1｜智能配置向导（单轮 LLM，无 agent loop）

```
用户输入意图 ──► /chat/sessions/{id}/interpret
                 │
                 ▼
          recommend.py: 单轮 LLM 调用
          (llm_router.py 无 tools 版)
                 │
                 ▼
          EvaluateRequest 草稿 (落库 ChatSession.draft_request)
                 │
                 ▼
          前端 EvalDraftPreviewCard 展示
                 │
                 ▼
          用户确认/修改 ──► /chat/sessions/{id}/draft (PATCH)
                 │
                 ▼
          /chat/sessions/{id}/apply ──► 现有 POST /evaluate
```

- **新增**：`chat/session.py`、`chat/recommend.py`、`chat/llm_router.py`（精简版）、2 张表、4 条 API、`ChatPage` + `EvalDraftPreviewCard`。
- **不做**：agent loop、工具集、流式。
- **价值**：立刻消除"15+ 字段不会填"痛点；为阶段 2 铺好表结构、API 骨架、前端入口。

### 阶段 2｜完整 AI 对话框（agent loop + 工具集）

```
用户消息 ──► /chat/sessions/{id}/messages
                │
                ▼
         orchestrator.py agent loop:
         ┌─► llm_router.chat_completion_with_tools(messages, tools)
         │     │
         │     ▼
         │   有 tool_calls? ──是──► 执行 tools.py 对应函数
         │     │                       │
         │     │                       ▼
         │     │                 结果回填为 tool role 消息
         │     │                       │
         │   否                       │
         │     ▼                       │
         │   增量更新 ChatSession.     ◄─┘
         │   draft_request
         │     │
         │     ▼
         │   SSE 推送中间态给前端 (复用 emit_event 模式)
         └─◄─┘ (循环直到 LLM 不再调工具，产出最终草稿)
                │
                ▼
         前端 ToolCallBubble + 流式文本 + 最终 EvalDraftPreviewCard
```

- **新增**：`chat/tools.py`（前 8 个工具）、`chat/orchestrator.py`、`llm_router.py` 补 tool-calling、SSE 路由、`ToolCallBubble`、`StreamingText`。
- **关键约束**：agent loop 最大轮数（防死循环，建议 10）、单会话 LLM 调用走 `llm_client.py` 8 并发信号量（避免挤占评估主链路）、tool 执行超时。
- **价值**：真正"对话即配置"，LLM 主动追问缺失字段、推荐指标组合。

### 阶段 3｜智能生成（生成类工具）

- 追加 4 个生成类工具（见 4.2 表末）。
- 对话框可执行"根据这段需求文档生成 20 道评测题"、"为这个场景生成三层子步骤模板"、"调优这个 G-Eval 的 criteria"。
- **价值**：覆盖最专业的配置项（数据集/场景模板/judge prompt），把"懂行才能配"的门槛也降下来。

---

## 6. 关键技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 对话引擎模型 | 复用 `LLMModel` 表（默认 `is_default` 那个） | 省 API key 配置；对话也走已测通的模型 |
| tool-calling 协议 | OpenAI 原生 function calling | GLM-4.7/OpenAI/DeepSeek 均支持；`AsyncOpenAI` 已封装 |
| 对话编排并发 | 独立 `asyncio.create_task` + 现有 8 并发信号量 | 不阻塞评估主链路（CLAUDE.md 大数据量准则） |
| SSE 管道 | 新增 `chat_streams` Queue 字典，仿 `task_streams` | 同进程限制一致；复用成熟模式 |
| 草稿落库 | `ChatSession.draft_request` JSON | 用户可中断、可恢复、可改后重跑 |
| 启动评测 | `apply` 内部直接调 `evaluate` 的 async 实现函数 | 免二次 HTTP，避免 token 校验绕一圈 |
| MCP 暴露 | `chat/tools.py` 同源函数 + `mcp_server.py` thin wrapper | 一次实现，IDE/对话两处用 |
| 前端路由 | `App.tsx` 加 `chat` TabType，挂"评估功能"分组 | 与现有导航一致，零重构 |

---

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| LLM 选错指标组合（如离线模式选 `enable_stability`） | `build_eval_request` 工具内置 schema 校验（对照 `EvaluateRequest` 约束）；前端 `EvalDraftPreviewCard` 必须人工确认才能 `apply` |
| agent loop 死循环 | 最大轮数硬上限（10）+ 单会话 LLM 调用累计 token 上限 |
| 大量对话历史拖慢查询 | `ChatMessage` 按 `session_id` 索引；会话列表分页；超 N 天的会话归档 |
| 单进程 SSE 限制（`tasks.py:16-21` 警告） | 对话编排同进程部署；若未来多 Worker，统一改 Redis Pub/Sub（评估 SSE 和对话 SSE 一起改） |
| 对话 LLM 费用 | 复用 `LLMModel`，对话调用计入 Langfuse trace（复用 `langfuse_trace_extract` 链路） |
| AI 出错无法降级 | `EvalPage` 表单保留，草稿可回填人工修改；`/apply` 失败回退到草稿态 |

---

## 8. 工作量量级（粗估，非时间预估）

| 模块 | 量级 |
|---|---|
| 阶段 1 全部（表+API+recommend+前端 ChatPage 草稿卡） | 中 |
| 阶段 2 后端（orchestrator+8工具+SSE+tool-calling） | 中-大 |
| 阶段 2 前端（流式 UI+工具气泡） | 中 |
| 阶段 3（4 个生成工具） | 中 |
| MCP 反哺 + 测试 | 小-中 |

---

## 9. 关键文件索引

| 用途 | 路径 |
|---|---|
| 任务创建表单 | `frontend/src/components/EvalPage.tsx` |
| EvaluateRequest schema | `backend/schemas.py:183-216` |
| EvaluationTask 模型 | `backend/database.py:68-103` |
| LLMModel 模型 | `backend/database.py:655-670` |
| LLM 客户端（8 并发信号量） | `backend/evaluators/llm_client.py:65,84-110` |
| SSE 进程内 Queue | `backend/tasks.py:16-34` |
| 创建任务路由 | `backend/main.py:1244-1412` |
| MCP server | `mcp/mcp_server.py` |
| MCP HTTP 客户端 | `mcp/backend_client.py` |
| 指标注册表 | `backend/evaluators/registry.py` |
| 场景评测服务 | `backend/services/scenario_eval_service.py` |
| 前端路由/TabType | `frontend/src/App.tsx:26-43` |
