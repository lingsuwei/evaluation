# AI 对话式配置 — 阶段 2 技术方案（完整 AI 对话框）

> **版本**：v1.0
> **日期**：2026-08-14
> **状态**：待实施
> **上级方案**：[AI对话式配置-整体架构方案.md](./AI对话式配置-整体架构方案.md)
> **前置阶段**：[AI对话式配置-阶段1技术方案.md](./AI对话式配置-阶段1技术方案.md)（已实施）
> **阶段定位**：agent loop + tool-calling + SSE 流式，LLM 主动追问缺失字段、推荐指标组合

---

## 1. 范围与边界

### 1.1 做什么

在阶段 1 的单轮意图解析之上，新增**多轮 agent loop**：LLM 通过 OpenAI function calling 主动调用配置类工具（查数据集、查 Agent、查指标、推荐、组装草稿），逐步收集缺失字段并填充 `EvaluateRequest` 草稿，全程通过 SSE 流式推送中间态给前端。

用户体验：
- 输入"评估 agent-001 在 ds_faq 上的忠实度"
- 前端流式显示：`正在查询数据集…` → `找到 ds_faq（5题）` → `正在查询 Agent…` → `确认 agent-001 在线` → `正在组装配置…` → `已生成草稿，请确认`
- 草稿就绪后展示 `EvalDraftPreviewCard`（阶段 1 已有），用户确认/修改/启动

### 1.2 不做什么（留给阶段 3）

- ❌ 生成评测集（question/ground_truth/context 生成）
- ❌ 生成场景子步骤模板
- ❌ 调优 G-Eval judge prompt
- ❌ `stream=True` 逐 token 打字机效果（阶段 2 的 LLM 调用是阻塞式完整返回，SSE 推送工具调用中间态；打字机效果作为可选增强后续再做）

### 1.3 与阶段 1 的关系

阶段 1 的单轮 `interpret` 路由**保留**（快速模式，适合意图明确的场景）。阶段 2 新增多轮 `stream` 路由（对话模式，适合意图模糊、需 LLM 主动补全的场景）。两者共存：

| 场景 | 用阶段 1 interpret | 用阶段 2 stream |
|---|---|---|
| 用户意图完整（含数据集/Agent/指标/轮次） | ✓ 快速单轮 | 也可用，但多余 |
| 用户意图模糊（只说"随便评一下"） | ✓ 但草稿缺字段多 | ✓ LLM 主动追问补全 |
| 用户想多轮对话修改 | ✗ 单轮无法 | ✓ 多轮追加消息 |

---

## 2. 数据流

```
用户输入消息 ──► POST /chat/sessions/{id}/messages {content}
                     │
                     ▼
               追加 user 消息到 ChatMessage 表
                     │
                     ▼
前端建立 ──► GET /chat/sessions/{id}/stream (EventSource)
                     │
                     ▼
          orchestrator.py agent loop 启动:
          ┌─► emit chat_start
          │
          │   加载历史 ChatMessage + 注入 system prompt(含工具清单)
          │
          ├─► llm_router.chat_completion_with_tools(messages, tools)
          │     │
          │     ▼
          │   response.choices[0].message
          │     │
          │     ├─ 有 tool_calls?
          │     │   │
          │     │   是├─► emit chat_tool_call (工具名+参数)
          │     │   │      │
          │     │   │      ▼
          │     │   │   执行 chat/tools.py 对应函数 (调现有 API)
          │     │   │      │
          │     │   │      ▼
          │     │   │   emit chat_tool_result (工具返回摘要)
          │     │   │      │
          │     │   │      ▼
          │     │   │   append_message(role=tool, tool_name, tool_args, tool_result)
          │     │   │      │
          │     │   │      ▼
          │     │   │   build_eval_request 工具: 增量更新 ChatSession.draft_request
          │     │   │      │
          │     │   └──────┘ (回到 LOOP 顶部)
          │     │
          │     │   否
          │     │   ▼
          │     │   assistant 文本消息
          │     │   │
          │     │   ▼
          │     │   append_message(role=assistant, content)
          │     │   │
          │     │   ▼
          │     │   emit chat_message (完整文本)
          │     │   │
          │     └───┘
          │
          └─► emit chat_complete (草稿已就绪)
                  │
                  ▼
          前端收到 chat_complete:
          - 渲染最终 assistant 文本
          - 拉取最新 ChatSession.draft_request → 展示 EvalDraftPreviewCard
          - close EventSource

用户在卡片上确认/修改 → PATCH /chat/sessions/{id}/draft (阶段1已有)
用户点"直接启动"   → POST /chat/sessions/{id}/apply (阶段1已有)
```

---

## 3. 后端模块设计

### 3.1 `backend/chat/tools.py` — 配置类工具集

7 个工具，写成纯 `async` 函数。每个工具有两部分：
- **OpenAI function schema**（给 LLM 看的声明）
- **执行函数**（实际调用，返回精简结果给 LLM）

工具分两类：
- **查询类**（list_*/get_*/recommend_*）：调现有 API，返回精简结构
- **动作类**（build_eval_request）：组装草稿并落库 `ChatSession.draft_request`

> `create_eval_task`（启动评测）**不作为 LLM 工具**——保持阶段 1 的 `apply` 按钮人工确认，LLM 只负责产草稿，不触碰启动。安全优先。

#### 工具清单

| # | 工具名 | 类型 | 对接现有 | 作用 |
|---|---|---|---|---|
| 1 | `list_datasets` | 查询 | `GET /datasets`（内部函数调用，免 HTTP） | 列出所有数据集（id/name/题数） |
| 2 | `list_agents` | 查询 | `GET /agents` 过滤 active | 列出可用 Agent（id/name/版本） |
| 3 | `list_llm_models` | 查询 | `GET /llm-models` | 列出已配置 LLM 模型（key/name/默认） |
| 4 | `list_metrics` | 查询 | 复用 `GET /metrics` 逻辑 | 列出可用评测指标（key/中文名/类别） |
| 5 | `get_dataset_preview` | 查询 | `GET /datasets/{id}` | 预览数据集字段（题数/scenario_key/字段分布） |
| 6 | `recommend_metrics` | 查询 | `recommend.py` 规则 | 按意图（在线/离线/评什么）推荐指标组合 |
| 7 | `build_eval_request` | 动作 | 写 `ChatSession.draft_request` | 组装草稿字段，增量更新落库 |

#### 工具 schema 设计（OpenAI function 格式）

每个工具 schema 形如：
```python
{
    "type": "function",
    "function": {
        "name": "list_datasets",
        "description": "列出系统中所有可用的评测数据集，返回每个数据集的 ID、名称和题目数量。",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
}
```

7 个工具的完整 schema 见 [附录 A](#附录-a工具-schema-清单)。

#### 工具执行函数对接点

工具函数**同进程直接调现有 service 函数**（免 HTTP，不同于 MCP 的走 `backend_client`）。对接关系：

| 工具 | 对接函数 | 精简返回（给 LLM） |
|---|---|---|
| `list_datasets` | `session.execute(select(Dataset))` | `[{id, name, item_count}]` |
| `list_agents` | `session.execute(select(Agent).where(Agent.is_active==True))` | `[{id, name, logical_agent_id, version}]` |
| `list_llm_models` | `session.execute(select(LLMModel))` | `[{key:id, name, is_default}]` |
| `list_metrics` | 复用 `main.py:list_metrics` 的分组逻辑 | `[{key, label, category}]`（三组合并扁平化） |
| `get_dataset_preview` | `select(Dataset).where(id==...)` + 解析 data JSON | `{id, name, item_count, fields:[...], scenario_keys:[...]}` |
| `recommend_metrics` | `recommend.py` 规则函数 | `{recommended:[...], reason:"..."}` |
| `build_eval_request` | `session.update_draft(session_id, fields)` | `{updated_fields:[...], draft:{...}}` |

> **精简原则**：工具返回给 LLM 的结构只含决策必需字段，剔除 `created_at`/`inference_config`/`api_key` 等 LLM 不需要的大字段，省 token。

### 3.2 `backend/chat/orchestrator.py` — Agent Loop

核心状态机：

```
INIT → LOOP(最多 N 轮) → DONE / ERROR
```

```python
async def run_agent_loop(
    session_id: str,
    model_key: str | None,
    max_rounds: int = 10,
) -> None:
    """运行 agent loop，通过 SSE 推送中间态，结束后草稿落库。

    每轮：
    1. 加载历史 ChatMessage + system prompt(含工具清单)
    2. 调 llm_router.chat_completion_with_tools(messages, tools)
    3. 若有 tool_calls：执行工具 → append tool 消息 → emit chat_tool_call/result → 回到 1
    4. 若无 tool_calls：append assistant 消息 → emit chat_message → 结束
    """
```

**关键设计**：

| 设计点 | 选择 | 理由 |
|---|---|---|
| 最大轮数 | 10（硬上限） | 防死循环；正常 2-4 轮即可完成配置 |
| 工具执行 | 串行（非并行） | 工具间可能有依赖（先查数据集再组装草稿），串行更可控 |
| 草稿更新 | `build_eval_request` 工具直接写 `draft_request` | LLM 的"输出"即 tool_call arguments，orchestrator 执行落库，LLM 不直接碰草稿 JSON |
| LLM 调用 | 阻塞式完整返回（非 stream） | 简化实现；stream=True 逐 token 留作可选增强 |
| 单会话 token 上限 | 累计 50000 tokens 超限即停 | 防失控；正常配置对话 <5000 tokens |
| 并发限流 | 复用 `llm_client._LLM_CALL_SEMAPHORE`（8 并发） | 不挤占评估主链路 |
| system prompt | 含工具清单 + 当前会话状态 + 规则 | 每轮固定注入 |

**system prompt 设计**（核心）：

```
你是 onto-eval 本体评测平台的 AI 配置助手。用户会用自然语言描述评测需求，
你需要通过调用工具收集信息并组装评测任务配置草稿。

可用工具：
- list_datasets: 列出所有数据集
- list_agents: 列出可用 Agent
- list_llm_models: 列出已配置的评测 LLM 模型
- list_metrics: 列出可用评测指标
- get_dataset_preview: 预览数据集字段
- recommend_metrics: 按意图推荐指标组合
- build_eval_request: 组装评测配置草稿（每收集到一个字段就可以调用，增量更新）

工作流程：
1. 用户描述意图后，先用 list_* 工具确认数据集/Agent/模型存在
2. 用 recommend_metrics 获取指标推荐
3. 用 build_eval_request 组装草稿（可多次调用，每次增量更新字段）
4. 草稿完整后，用自然语言告诉用户"已生成配置草稿，请确认"

规则：
- 在线评估(online_mode=true)必须有 agent_id
- 离线评估不需要 agent_id，enable_stability 建议设 false
- 用户没明确在线/离线，默认在线
- 轮次 inference_rounds 限制 1-10
- 不要编造不存在的 dataset_id/agent_id，先查再组装
- 回复用中文，简洁
```

### 3.3 `backend/chat/llm_router.py` — 扩展 tool-calling

阶段 1 的 `chat_completion` 保留。新增 `chat_completion_with_tools`：

```python
async def chat_completion_with_tools(
    messages: list[dict],
    tools: list[dict],
    model_key: str | None = None,
) -> dict:
    """带 tool-calling 的 LLM 调用，返回原始 response 对象。

    复用 get_llm_client + _LLM_CALL_SEMAPHORE(8并发) + 429重试。
    不做 stream，阻塞式完整返回。

    Args:
        messages: OpenAI 消息格式 [{role, content}, {role, tool, tool_call_id, ...}]
        tools: OpenAI function schema 清单
        model_key: 模型标识

    Returns:
        response 对象（含 choices[0].message.tool_calls）
    """
```

- **不复用 `evaluate_with_llm`**（它只支持 system+user 两消息、无 tools）。直接用 `get_llm_client` 拿 `AsyncOpenAI`，自己写 `chat.completions.create(messages=..., tools=..., tool_choice="auto")` + 信号量 + 429 重试。
- 429 重试逻辑参照 `llm_client.py:307-312` 的 `_call_single_model` 实现。

### 3.4 SSE 基础设施

**照搬 `tasks.py:16-34` 的 `task_streams` 模式**，新增 `chat_streams`：

```python
# backend/chat/orchestrator.py 顶部

# ⚠️ WARNING: SSE 流存储（进程内内存字典）
# 同 task_streams，不支持多 Worker / 多实例部署。
chat_streams: Dict[str, asyncio.Queue] = {}


async def get_chat_queue(session_id: str) -> asyncio.Queue:
    if session_id not in chat_streams:
        chat_streams[session_id] = asyncio.Queue()
    return chat_streams[session_id]


async def emit_chat_event(session_id: str, event_type: str, data: dict):
    queue = await get_chat_queue(session_id)
    await queue.put({"event": event_type, "data": data})
```

> 必须保留单进程限制警告注释（`tests/test_recent_changes.py:234-242` 有测试守卫此注释存在）。

### 3.5 SSE 路由

**照搬 `main.py:2295-2312` 的 `/stream/{task_uuid}` 模式**：

```python
@app.get("/chat/sessions/{session_id}/stream")
async def chat_stream(session_id: str):
    """SSE: 推送 agent loop 中间态。"""
    async def event_generator():
        queue = await get_chat_queue(session_id)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=60.0)
                yield {
                    "event": event["event"],
                    "data": json.dumps(event["data"], ensure_ascii=False)
                }
                if event["event"] in ["chat_complete", "chat_error"]:
                    break
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": "{}"}

    return EventSourceResponse(event_generator())
```

### 3.6 API 路由新增/变更

| 路由 | 方法 | 说明 | 阶段 |
|---|---|---|---|
| `/chat/sessions/{id}/messages` | POST | 追加用户消息（阶段 1 已建，阶段 2 正式启用多轮） | 2 |
| `/chat/sessions/{id}/stream` | GET | SSE 流式 agent loop（**新增**） | 2 |
| `/chat/sessions/{id}/interpret` | POST | 阶段 1 单轮快速模式（**保留**） | 1 |

`POST /chat/sessions/{id}/messages` 的行为变更：阶段 1 仅追加消息；阶段 2 追加后**异步启动 agent loop**（`asyncio.create_task(run_agent_loop(...))`），前端通过 `stream` 路由消费 SSE。

> **启动方式**：`POST /messages` 追加消息后 `create_task` 启动 loop（不阻塞 HTTP 响应），前端随即 `GET /stream` 消费。loop 通过 `chat_streams` Queue 推送事件，`stream` 路由消费。两者解耦，loop 出错不影响前端已收到的消息。

---

## 4. Agent Loop 状态机详解

```
                    ┌─────────┐
                    │  INIT   │
                    └────┬────┘
                         │ emit chat_start
                         ▼
              ┌──────────────────────┐
              │ 加载历史 ChatMessage  │
              │ + 注入 system prompt  │
              │ + 注入工具 schema 清单 │
              └──────────┬───────────┘
                         │
                ┌────────▼────────┐
                │  LOOP 第 n 轮   │◄─────────────┐
                │ (n <= max_rounds)│              │
                └────────┬────────┘              │
                         │                       │
                  调 llm_router                  │
              chat_completion_with_tools          │
                         │                       │
                         ▼                       │
              ┌─────────────────────┐            │
              │ response.message     │            │
              │ .tool_calls 为空?    │            │
              └────┬──────────┬──────┘            │
                   │          │                   │
                  否          是                   │
                   │          │                   │
                   ▼          ▼                   │
         ┌─────────────┐  ┌──────────────┐        │
         │ 遍历        │  │ append        │        │
         │ tool_calls  │  │ assistant 消息│        │
         ├─────────────┤  │ (content)     │        │
         │emit         │  └──────┬───────┘        │
         │chat_tool_call│         │                │
         ├─────────────┤         │                │
         │执行 tools.py│         │                │
         │对应函数     │         │                │
         ├─────────────┤         │                │
         │emit         │         │                │
         │chat_tool_   │         │                │
         │result       │         │                │
         ├─────────────┤         │                │
         │append_message│        │                │
         │(role=tool)  │         │                │
         ├─────────────┤         │                │
         │build_eval_  │         │                │
         │request 工具 │         │                │
         │→ 更新草稿   │         │                │
         └──────┬──────┘         │                │
                │                │                │
                └────────────────┼────────────────┘
                                 │ (回到 LOOP)
                                 │
                                 ▼
                         ┌──────────────┐
                         │ emit         │
                         │ chat_message │
                         │ (最终文本)   │
                         └──────┬───────┘
                                │
                                ▼
                         ┌──────────────┐
                         │ emit         │
                         │ chat_complete│
                         │ (草稿就绪)   │
                         └──────┬───────┘
                                │
                                ▼
                            ┌───────┐
                            │ DONE  │
                            └───────┘

  异常分支（任何步骤出错 / 超过 max_rounds / token 超限）:
                                ▼
                         ┌──────────────┐
                         │ emit         │
                         │ chat_error   │
                         └──────┬───────┘
                                ▼
                            ┌───────┐
                            │ ERROR │
                            └───────┘
```

---

## 5. SSE 事件类型清单

| 事件类型 | 触发时机 | data 字段 |
|---|---|---|
| `chat_start` | agent loop 启动 | `{session_id, model_key, max_rounds}` |
| `chat_tool_call` | LLM 决定调用工具 | `{tool_name, tool_args, round}` |
| `chat_tool_result` | 工具执行返回 | `{tool_name, result_summary, round}` |
| `chat_message` | LLM 产出最终文本（无 tool_calls） | `{content, round}` |
| `chat_complete` | agent loop 正常结束 | `{session_id, draft_ready: bool}` |
| `chat_error` | 出错/超轮/token超限 | `{error, round}` |
| `ping` | 60s 超时心跳 | `{}` |

---

## 6. 前端设计

### 6.1 ChatPage 改造

阶段 1 的 ChatPage 是纯 REST（`axios.post` 同步等 interpret）。阶段 2 改造为**支持两种模式**：

- **快速模式**（阶段 1 保留）：点"解析配置" → `POST /interpret` → 同步等草稿
- **对话模式**（阶段 2 新增）：点"AI 对话" → 输入消息 → `POST /messages` + `GET /stream` → 流式渲染

ChatPage 新增状态：
```typescript
interface ChatLine {
  id: number
  role: 'user' | 'assistant' | 'tool'
  content: string
  toolName?: string
  toolArgs?: Record<string, any>
  toolResult?: any
  round?: number
  type: 'message' | 'tool_call' | 'tool_result' | 'error'
}

const [chatLines, setChatLines] = useState<ChatLine[]>([])
const [streaming, setStreaming] = useState(false)
const eventSourceRef = useRef<EventSource | null>(null)
```

**EventSource 建立逻辑**（复用 `TaskMonitor.tsx:301-650` 模式）：

```typescript
const startStream = (sessionId: string) => {
  setStreaming(true)
  const es = new EventSource(`${API_BASE}/chat/sessions/${sessionId}/stream`)
  eventSourceRef.current = es

  es.addEventListener('chat_start', (e) => { /* ... */ })
  es.addEventListener('chat_tool_call', (e) => {
    const data = safeParseSSE(e, 'chat_tool_call')
    setChatLines(prev => [...prev, {
      id: Date.now(), role: 'tool', type: 'tool_call',
      toolName: data.tool_name, toolArgs: data.tool_args, round: data.round,
      content: `调用工具：${data.tool_name}`
    }])
  })
  es.addEventListener('chat_tool_result', (e) => { /* 追加 tool_result 行 */ })
  es.addEventListener('chat_message', (e) => { /* 追加 assistant 文本 */ })
  es.addEventListener('chat_complete', (e) => {
    setStreaming(false)
    fetchDetail(sessionId)  // 拉取最新草稿展示卡片
    es.close()
  })
  es.addEventListener('chat_error', (e) => {
    setStreaming(false)
    setError(safeParseSSE(e, 'chat_error')?.error)
    es.close()
  })
  es.onerror = () => { /* 指数退避重连，复用 TaskMonitor 模式 */ }
}
```

### 6.2 StreamingText 组件

可选增强（阶段 2 可暂不实现）：若后端启用 `stream=True`，此组件逐 token 渲染。

阶段 2 的 `chat_message` 事件是完整文本一次性推送，前端直接渲染即可，无需 StreamingText。组件预留，后续启用 stream=True 时再用。

### 6.3 ToolCallBubble 组件

渲染工具调用气泡（`chat_tool_call` + `chat_tool_result` 事件）：

```typescript
function ToolCallBubble({ line }: { line: ChatLine }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <div className={`chat-bubble chat-bubble-tool`}>
      <div className="chat-bubble-header" onClick={() => setExpanded(e => !e)}>
        <span className="chat-tool-icon">🔧</span>
        <span>{line.toolName}</span>
        <span className="chat-bubble-toggle">{expanded ? '收起' : '展开'}</span>
      </div>
      {expanded && (
        <div className="chat-bubble-detail">
          <div className="chat-tool-args">参数：{JSON.stringify(line.toolArgs)}</div>
          <div className="chat-tool-result">结果：{JSON.stringify(line.toolResult)}</div>
        </div>
      )}
    </div>
  )
}
```

### 6.4 ChatLine 渲染

```typescript
{chatLines.map(line => {
  if (line.type === 'tool_call' || line.type === 'tool_result') {
    return <ToolCallBubble key={line.id} line={line} />
  }
  return (
    <div key={line.id} className={`chat-bubble chat-bubble-${line.role}`}>
      {line.content}
    </div>
  )
})}
{streaming && <div className="chat-typing"><div className="chat-spinner" /></div>}
```

### 6.5 样式

`frontend/src/index.css` 追加 chat 对话气泡样式（复用 `--edp-*` 变量）：
- `.chat-bubble` / `.chat-bubble-user` / `.chat-bubble-assistant` / `.chat-bubble-tool`
- `.chat-bubble-header` / `.chat-bubble-detail` / `.chat-tool-icon`
- `.chat-typing`（流式中 spinner）

---

## 7. 数据模型

**无新增表**。阶段 1 的 `ChatSession` / `ChatMessage` 已预留所有字段：

| 字段 | 阶段 1 用法 | 阶段 2 用法 |
|---|---|---|
| `ChatMessage.role` | user / assistant | 扩展 tool 角色 |
| `ChatMessage.tool_name` | 预留未用 | 工具名 |
| `ChatMessage.tool_args` | 预留未用 | 工具参数 JSON |
| `ChatMessage.tool_result` | 预留未用 | 工具返回 JSON |
| `ChatSession.draft_request` | 单轮 interpret 写入 | agent loop 增量更新 |
| `ChatSession.status` | draft→ready→consumed | 同（loop 结束置 ready） |

**无 alembic 迁移**。

---

## 8. 实施任务清单

### 8.1 后端

| # | 任务 | 产出 | 验收 |
|---|---|---|---|
| B2-1 | `chat/tools.py`：7 个工具的 schema + 执行函数 | `backend/chat/tools.py` | 每个工具单测：mock 数据库调用返回精简结构 |
| B2-2 | `chat/llm_router.py`：`chat_completion_with_tools` | `backend/chat/llm_router.py` 扩展 | mock AsyncOpenAI 验证 tools 参数传入、tool_calls 解析 |
| B2-3 | `chat/orchestrator.py`：`run_agent_loop` + `chat_streams` Queue + `emit_chat_event` | `backend/chat/orchestrator.py` | 单进程限制警告注释存在（test_recent_changes 守卫） |
| B2-4 | `main.py`：`GET /chat/sessions/{id}/stream` SSE 路由 | `main.py` 追加 | EventSource 能连上，收到 ping 心跳 |
| B2-5 | `main.py`：`POST /chat/sessions/{id}/messages` 改造为追加+异步启动 loop | `main.py` 修改现有路由 | 追加消息后 loop 在后台启动，SSE 推送事件 |
| B2-6 | system prompt 含工具清单 + 规则 | `orchestrator.py` 内 | LLM 能正确调工具 |

### 8.2 前端

| # | 任务 | 产出 | 验收 |
|---|---|---|---|
| F2-1 | ChatPage 加对话模式开关 + chatLines state | `ChatPage.tsx` 改造 | 快速/对话两种模式可切换 |
| F2-2 | EventSource 接收逻辑（复用 TaskMonitor 模式） | `ChatPage.tsx` | 收到 7 种事件正确渲染 |
| F2-3 | ToolCallBubble 组件 | `frontend/src/components/ToolCallBubble.tsx` | 工具调用气泡可展开看参数/结果 |
| F2-4 | chat_complete 后拉取草稿展示 EvalDraftPreviewCard | `ChatPage.tsx` | loop 结束自动展示卡片（阶段 1 组件复用） |
| F2-5 | 流式中 spinner + 错误展示 | `ChatPage.tsx` | streaming 态有指示，出错显示错误 |
| F2-6 | 样式追加 | `index.css` | 对话气泡/工具气泡/spinner 样式就绪 |

### 8.3 测试与联调

| # | 任务 | 验收 |
|---|---|---|
| T2-1 | 后端单测：tools.py 7 个工具 | 每个工具 mock DB 返回正确精简结构 |
| T2-2 | 后端单测：orchestrator agent loop（mock LLM） | 2 轮 loop 正常结束，草稿落库 |
| T2-3 | 端到端：意图"评估 agent-001 在 ds_faq 忠实度跑3轮" | LLM 调 list_datasets→list_agents→build_eval_request→产出文本，草稿正确 |
| T2-4 | 端到端：模糊意图"随便评一下" | LLM 主动追问/调工具补全，missing_fields 收敛 |
| T2-5 | 超轮保护：mock LLM 永远调工具 | 10 轮后 emit chat_error，不死循环 |
| T2-6 | SSE 断连重连：手动断网恢复 | 前端指数退避重连，不丢草稿 |

---

## 9. 验收标准

1. 用户在对话模式输入意图，前端流式显示工具调用过程（工具名→结果摘要）
2. LLM 主动调 `list_*` 工具确认资源存在，不编造不存在的 ID
3. `build_eval_request` 工具增量更新草稿，loop 结束后 `EvalDraftPreviewCard` 展示完整草稿
4. 意图模糊时 LLM 主动追问或调工具补全，最终草稿 `missing_fields` 为空或少
5. 超过 10 轮自动 emit `chat_error`，不死循环
6. SSE 断连后前端重连，草稿不丢（已落库 `draft_request`）
7. 阶段 1 的 `interpret` 快速模式仍可用，零回归
8. 现有 `POST /evaluate` 和评估主链路零影响

---

## 10. 风险与对策

| 风险 | 对策 |
|---|---|
| LLM 编造不存在的 dataset_id/agent_id | system prompt 强制"先查再组装"；`build_eval_request` 工具校验 dataset_id 存在性（不存在返回错误给 LLM） |
| LLM 死循环不停调工具 | `max_rounds=10` 硬上限 + 累计 token 上限 50000 |
| tool-calling 兼容性（GLM vs OpenAI） | OpenAI function calling 是事实标准，GLM-4.7/OpenAI/DeepSeek 均兼容；若个别模型不支持，降级到阶段 1 单轮 interpret |
| SSE 单进程限制 | 同 `task_streams` 限制；未来多 Worker 时统一改 Redis Pub/Sub（评估 SSE + 对话 SSE 一起改） |
| agent loop 阻塞事件循环 | `asyncio.create_task` 异步启动，不阻塞 `POST /messages` 响应；loop 内 LLM �用走 8 并发信号量不挤占评估 |
| 工具执行超时 | 每个工具调用加 `asyncio.wait_for(timeout=30)`，超时返回错误给 LLM |
| 用户中途发新消息打断 loop | 检测 session 是否有新 user 消息，有则终止当前 loop（可选增强，初版可不做） |

---

## 11. 与阶段 3 的衔接

阶段 2 产出的 agent loop + 工具集 + SSE 是阶段 3 的基础，阶段 3 纯追加：

| 阶段 2 产出 | 阶段 3 如何扩展 |
|---|---|
| `tools.py` 7 个工具 | 追加 4 个生成类工具（generate_dataset / generate_scenario_template / recommend_verdict_thresholds / tune_judge_prompt） |
| `orchestrator.py` agent loop | 不变，新增工具自动被 LLM 调用 |
| `llm_router.chat_completion_with_tools` | 不变 |
| SSE 事件 + 前端 ToolCallBubble | 不变，生成类工具复用同一套渲染 |
| `ChatMessage` 表 | 不变（tool_* 字段已足够） |

阶段 3 不重写任何阶段 2 设计，纯追加工具函数。

---

## 12. 关键文件索引

| 用途 | 路径 | 行号 |
|---|---|---|
| SSE 路由模板（照搬） | `backend/main.py` | 2295-2312 |
| task_streams Queue 模板（照搬） | `backend/tasks.py` | 16-34 |
| emit_event 封装示例 | `backend/services/evaluation_pipeline.py` | 64-123 |
| 单进程限制测试守卫 | `backend/tests/test_recent_changes.py` | 234-242 |
| LLM 客户端 + 信号量 | `backend/evaluators/llm_client.py` | 65,84,276-285 |
| 阶段 1 llm_router（扩展基础） | `backend/chat/llm_router.py` | 全文 |
| 阶段 1 session CRUD（复用） | `backend/chat/session.py` | append_message:228-256 |
| 阶段 1 recommend（复用规则） | `backend/chat/recommend.py` | 全文 |
| 阶段 1 eval_task_service（apply 复用） | `backend/services/eval_task_service.py` | 全文 |
| ChatMessage 表（已预留 tool_*） | `backend/database.py` | 753-772 |
| 前端 SSE 接收模板（复用） | `frontend/src/components/TaskMonitor.tsx` | 7-14,301-650 |
| 阶段 1 ChatPage（改造基础） | `frontend/src/components/ChatPage.tsx` | 全文 |
| 阶段 1 EvalDraftPreviewCard（复用） | `frontend/src/components/EvalDraftPreviewCard.tsx` | 全文 |

---

## 附录 A：工具 Schema 清单

### A.1 list_datasets

```json
{
  "type": "function",
  "function": {
    "name": "list_datasets",
    "description": "列出系统中所有可用的评测数据集，返回每个数据集的 ID、名称和题目数量。当用户提到数据集名称但不确定 ID 时调用。",
    "parameters": {
      "type": "object",
      "properties": {},
      "required": []
    }
  }
}
```
**返回**：`[{id, name, item_count}]`

### A.2 list_agents

```json
{
  "type": "function",
  "function": {
    "name": "list_agents",
    "description": "列出所有已激活的 Agent，返回 ID、名称、逻辑AgentID、版本。在线评估需要 agent_id 时调用。",
    "parameters": {
      "type": "object",
      "properties": {},
      "required": []
    }
  }
}
```
**返回**：`[{id, name, logical_agent_id, version}]`（仅 is_active=true）

### A.3 list_llm_models

```json
{
  "type": "function",
  "function": {
    "name": "list_llm_models",
    "description": "列出已配置的评测 LLM 模型，返回模型 key、显示名称、是否默认。用于确定 llm_model 字段。",
    "parameters": {
      "type": "object",
      "properties": {},
      "required": []
    }
  }
}
```
**返回**：`[{key, name, is_default}]`

### A.4 list_metrics

```json
{
  "type": "function",
  "function": {
    "name": "list_metrics",
    "description": "列出所有可用的评测指标，返回指标 key、中文名、类别。用于确定 selected_metrics 字段。",
    "parameters": {
      "type": "object",
      "properties": {
        "evaluation_mode": {
          "type": "string",
          "enum": ["online", "offline"],
          "description": "评估模式，过滤该模式不支持的指标"
        }
      },
      "required": []
    }
  }
}
```
**返回**：`[{key, label, category}]`（builtin+deepeval+custom 扁平化合并）

### A.5 get_dataset_preview

```json
{
  "type": "function",
  "function": {
    "name": "get_dataset_preview",
    "description": "预览指定数据集的字段分布和场景键，确认数据集内容匹配评测需求。",
    "parameters": {
      "type": "object",
      "properties": {
        "dataset_id": {
          "type": "string",
          "description": "数据集 ID"
        }
      },
      "required": ["dataset_id"]
    }
  }
}
```
**返回**：`{id, name, item_count, fields: ["question","ground_truth",...], scenario_keys: ["generic_l0",...]}`

### A.6 recommend_metrics

```json
{
  "type": "function",
  "function": {
    "name": "recommend_metrics",
    "description": "根据评测意图推荐合适的指标组合。当用户不确定选哪些指标时调用。",
    "parameters": {
      "type": "object",
      "properties": {
        "online_mode": {
          "type": "boolean",
          "description": "是否在线评估"
        },
        "focus": {
          "type": "string",
          "description": "评测关注点，如'忠实度''正确性''相关性'，可逗号分隔多个"
        }
      },
      "required": ["online_mode"]
    }
  }
}
```
**返回**：`{recommended: ["faithfulness",...], reason: "..."}`

### A.7 build_eval_request

```json
{
  "type": "function",
  "function": {
    "name": "build_eval_request",
    "description": "组装评测任务配置草稿。每收集到一个字段就可以调用，会增量合并到草稿中。所有字段确认后即可完成配置。",
    "parameters": {
      "type": "object",
      "properties": {
        "dataset_id": {"type": "string", "description": "数据集 ID"},
        "online_mode": {"type": "boolean", "description": "是否在线评估"},
        "agent_id": {"type": "string", "description": "Agent ID（在线模式必填）"},
        "llm_model": {"type": "string", "description": "评测 LLM 模型 key"},
        "inference_rounds": {"type": "integer", "description": "推理轮次 1-10", "minimum": 1, "maximum": 10},
        "selected_metrics": {"type": "array", "items": {"type": "string"}, "description": "评估因子 key 列表"},
        "use_bm25": {"type": "boolean", "description": "是否启用 BM25 忠实度"},
        "use_doc_library": {"type": "boolean", "description": "是否使用文档库"},
        "ontology_package": {"type": "string", "description": "本体包名称"},
        "auto_scenario_score": {"type": "boolean", "description": "是否自动场景打分"},
        "enable_process_consistency": {"type": "boolean", "description": "是否评测过程一致性"},
        "enable_stability": {"type": "boolean", "description": "是否评测运行稳定性（仅在线有意义）"}
      },
      "required": []
    }
  }
}
```
**返回**：`{updated_fields: ["dataset_id",...], draft: {...}, missing_fields: [...]}`
**行为**：调 `session.update_draft(session_id, args)` 增量合并落库，返回更新后的草稿 + 仍缺失的必填字段。
