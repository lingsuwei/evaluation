# AI 对话式配置 — 阶段 1 技术方案（智能配置向导）

> **版本**：v1.0
> **日期**：2026-08-14
> **状态**：待实施
> **上级方案**：[AI对话式配置-整体架构方案.md](./AI对话式配置-整体架构方案.md)
> **阶段定位**：单轮 LLM 意图解析，无 agent loop，无 tool-calling，产出 `EvaluateRequest` 草稿可回填表单

---

## 1. 范围与边界

### 1.1 做什么

用户在 `ChatPage` 输入一句意图（如"用 GLM-4.7 评估 agent-001 在数据集 ds_faq 上的忠实度和正确性，跑 3 轮"），系统**单轮 LLM 调用**解析为 `EvaluateRequest` 草稿，前端以卡片展示，用户确认/修改后回填到 `EvalPage` 表单并启动评测。

### 1.2 不做什么（留给阶段 2/3）

- ❌ 多轮对话（无 agent loop，无 tool-calling 循环）
- ❌ 工具集（`list_datasets`/`list_agents` 等留到阶段 2）
- ❌ SSE 流式输出（阶段 1 是请求-响应式，非流式）
- ❌ 生成评测集/场景模板/judge prompt（阶段 3）
- ❌ 替换 `EvalPage` 表单（表单保留，AI 产出可回填、可人工改）

### 1.3 核心价值

立刻消除"15+ 字段不会填"痛点；为阶段 2 铺好表结构、API 骨架、前端入口、`llm_router` 基础。

---

## 2. 数据流

```
┌──────────┐   POST /chat/sessions       ┌──────────────┐
│ ChatPage │─────────────────────────────►│ ChatSession  │ (status=draft)
 │         │◄──────────────────────────── │ 新建会话      │
 │         │   {session_id}               └──────────────┘
 │         │
 │         │   POST /chat/sessions/{id}/interpret
 │         │   {intent_text: "..."}
 │         │─────────────────────────────►┌──────────────┐
 │         │                              │ recommend.py │
 │         │                              │  单轮 LLM 调用│
 │         │                              │  (llm_router)│
 │         │                              └──────┬───────┘
 │         │◄────────────────────────────        │
 │         │   {                              ┌──▼──────────────┐
 │         │     draft: EvaluateRequest,     │ ChatSession      │
 │         │     confidence: 0.9,            │ .draft_request   │
 │         │     missing_fields: [...],      │ 落库更新         │
 │         │     suggestions: [...]          │ status=ready    │
 │         │   }                             └─────────────────┘
 │         │
 │  渲染 EvalDraftPreviewCard
 │  用户改草稿字段 ──► PATCH /chat/sessions/{id}/draft
 │
 │  点"回填到表单并启动"
 │         │
 │         │   POST /chat/sessions/{id}/apply
 │         │─────────────────────────────►┌──────────────┐
 │         │                              │ 调用现有     │
 │         │                              │ evaluate 内部│
 │         │                              │ async 函数   │
 │         │                              └──────┬───────┘
 │         │◄────────────────────────────        │
 │         │   {task_uuid: "task_xxx"}          │
 │         │                                     ▼
 │         │                              ChatSession.status=consumed
 │         │
 │         │   跳转 EvalPage (预填 draft) 或直接跳结果监控
```

---

## 3. 数据模型

### 3.1 新增表（`backend/database.py`）

```python
class ChatSession(Base):
    """AI 对话配置会话"""
    __tablename__ = "chat_sessions"

    id = Column(String, primary_key=True)  # "chat_xxx"
    title = Column(String, nullable=True)  # 自动生成（截断 intent_text 前 30 字）
    status = Column(String, default="draft")  # draft | ready | consumed | error
    intent = Column(String, nullable=True)  # online | offline | unknown
    model_key = Column(String, nullable=True)  # 对话引擎用的 LLMModel.id
    intent_text = Column(Text, nullable=True)  # 用户原始意图文本
    draft_request = Column(Text, nullable=True)  # JSON: EvaluateRequest 草稿
    confidence = Column(Float, nullable=True)  # LLM 解析置信度 0-1
    missing_fields = Column(Text, nullable=True)  # JSON: 缺失字段名列表
    error_message = Column(Text, nullable=True)  # 解析失败时的错误
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChatMessage(Base):
    """对话消息（阶段 1 仅存 interpret 的 user/assistant 两条；阶段 2 扩展为流式）"""
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String, nullable=False, index=True)  # FK chat_sessions.id
    role = Column(String, nullable=False)  # user | assistant | tool
    content = Column(Text, nullable=True)  # 消息文本
    tool_name = Column(String, nullable=True)  # 阶段 2 才用
    tool_args = Column(Text, nullable=True)  # JSON
    tool_result = Column(Text, nullable=True)  # JSON
    tokens = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
```

### 3.2 Alembic 迁移

文件：`backend/alembic/versions/029_chat_session_tables.py`

```python
"""create chat_sessions and chat_messages tables

Revision ID: 029_chat_session_tables
Revises: 028_migrate_judging_mode_names
Create Date: 2026-08-14
"""
revision: str = "029_chat_session_tables"
down_revision: Union[str, None] = "028_migrate_judging_mode_names"
```

- `chat_messages.session_id` 建索引（大数据量查询性能）
- `chat_messages.created_at` 建索引（按时间分页）
- 不建外键约束（与现有 `evaluation_tasks` 等表一致风格，无 FK）

---

## 4. 后端 API

### 4.1 `POST /chat/sessions` — 建会话

**请求体**：
```json
{
  "intent_text": "用 GLM-4.7 评估 agent-001 在 ds_faq 上的忠实度和正确性，跑 3 轮",
  "model_key": null
}
```
- `intent_text`：必填，用户意图原文
- `model_key`：可选，指定对话引擎模型；不填则用 `LLMModel.is_default=True` 的那个

**响应**：
```json
{
  "session_id": "chat_a1b2c3",
  "status": "draft"
}
```

### 4.2 `POST /chat/sessions/{id}/interpret` — 阶段 1 核心

**请求体**：空（用 `session.intent_text`）

**处理流程**：
1. 读 `ChatSession`，取 `intent_text` 和 `model_key`
2. 调 `recommend.interpret_intent(intent_text, model_key)` —— 单轮 LLM
3. 落库 `draft_request` / `confidence` / `missing_fields`，`status=ready`
4. 存两条 `ChatMessage`（user: intent_text, assistant: 解析结果摘要）
5. 返回草稿

**响应**：
```json
{
  "session_id": "chat_a1b2c3",
  "status": "ready",
  "draft": {
    "dataset_id": "ds_faq",
    "online_mode": true,
    "agent_id": "agent-001",
    "llm_model": "glm",
    "inference_rounds": 3,
    "selected_metrics": ["faithfulness", "correctness"],
    "use_bm25": false,
    "use_doc_library": false,
    "ontology_package": null,
    "auto_scenario_score": true,
    "enable_process_consistency": true,
    "enable_stability": true
  },
  "confidence": 0.85,
  "missing_fields": [],
  "suggestions": [
    "在线评估建议开启 enable_stability 评测运行稳定性维度",
    "faithfulness 指标如需精确匹配可考虑启用 BM25"
  ]
}
```

### 4.3 `PATCH /chat/sessions/{id}/draft` — 用户修改草稿

**请求体**（部分字段，合并到 `draft_request`）：
```json
{
  "inference_rounds": 5,
  "selected_metrics": ["faithfulness", "answer_relevancy", "correctness"]
}
```

**响应**：同 4.2 的 `draft` 全量。

### 4.4 `POST /chat/sessions/{id}/apply` — 启动评测

**请求体**：空（用 `session.draft_request`）

**处理流程**：
1. 校验 `draft_request` 为 `EvaluateRequest` 合法（Pydantic 解析）
2. 同进程调用现有 `evaluate` 路由的内部 async 函数（**不二次 HTTP**）
3. 成功后 `status=consumed`，返回 `task_uuid`
4. 失败 `status=error`，`error_message` 记录原因

**响应**（成功）：
```json
{ "task_uuid": "task_xxxx" }
```
**响应**（失败）：
```json
{ "error": "dataset ds_faq not found", "session_status": "error" }
```

### 4.5 其余路由

| 路由 | 方法 | 说明 |
|---|---|---|
| `/chat/sessions` | GET | 分页列表，支持 `status`/`intent` 过滤，`page`/`page_size` |
| `/chat/sessions/{id}` | GET | 详情（含 messages） |
| `/chat/sessions/{id}/messages` | POST | 追加用户消息（阶段 1 用得少，主要为阶段 2 预留） |
| `/chat/sessions/{id}` | DELETE | 删会话（级联删 messages） |

---

## 5. 后端模块设计

### 5.1 `backend/chat/llm_router.py`（阶段 1 精简版）

```python
async def chat_completion(
    system_prompt: str,
    user_prompt: str,
    model_key: str | None = None,
    response_json: bool = True,
) -> tuple[str, int]:
    """单轮 LLM 调用，返回 (文本, tokens)。

    阶段 1 不含 tool-calling。复用 get_llm_client + 8 并发信号量。
    response_json=True 时，要求模型返回 JSON（GLM/OpenAI/DeepSeek 均支持
    response_format={"type":"json_object"}）。

    Args:
        system_prompt: 系统提示词
        user_prompt: 用户提示词
        model_key: 模型标识，None 则用默认模型
        response_json: 是否强制 JSON 输出

    Returns:
        (content_text, total_tokens)
    """
```

- 复用 `get_llm_client(model_key)`（`llm_client.py:84`）拿 `AsyncOpenAI`
- 复用 `_LLM_CALL_SEMAPHORE`（`llm_client.py:65`，8 并发）
- 复用 429 重试逻辑（`_MAX_RETRIES=5`，`llm_client.py:68`）
- 阶段 2 扩展为 `chat_completion_with_tools(messages, tools, ...)` 加入 tool-calling

### 5.2 `backend/chat/recommend.py`

```python
async def interpret_intent(
    intent_text: str,
    model_key: str | None,
) -> dict:
    """单轮意图解析：自然语言 → EvaluateRequest 草稿。

    Returns:
        {
            "draft": {...},          # EvaluateRequest 字段
            "confidence": 0.85,
            "missing_fields": [],   # 必填但未识别的字段
            "suggestions": [...]    # 给用户的建议
        }
    """
```

**System Prompt 设计**（核心）：

```
你是 onto-eval 本体评测平台的配置助手。用户会用自然语言描述评测需求，
你需要把它解析为结构化的评测任务配置（EvaluateRequest）。

输出必须是 JSON，字段如下：
{
  "dataset_id": "数据集ID，用户没提则null",
  "online_mode": true/false,  // true=在线评估(调Agent), false=离线评估(用Langfuse Trace)
  "agent_id": "Agent ID，离线评估或用户没提则null",
  "llm_model": "模型key，如glm；用户没提则null",
  "inference_rounds": 1,  // 1-10，默认1
  "selected_metrics": ["faithfulness","answer_relevancy","correctness"],
  "use_bm25": false,
  "use_doc_library": false,
  "ontology_package": null,
  "auto_scenario_score": true,
  "enable_process_consistency": true,
  "enable_stability": true,
  "confidence": 0.0-1.0,  // 你对这次解析的置信度
  "missing_fields": ["必填但用户没提供的字段名"],
  "suggestions": ["给用户的建议，1-3条"]
}

可用指标说明：
- faithfulness: 忠实度，回答是否基于上下文，不编造
- answer_relevancy: 答案相关性，是否切题
- correctness: 正确性，与标准答案对比

规则：
1. 在线评估(online_mode=true)必须有agent_id；离线评估不需要
2. 离线评估 enable_stability 通常无意义(无实时推理)，建议设false
3. 在线评估建议开启 enable_stability 评测运行稳定性
4. 用户说"跑N轮"映射 inference_rounds(1-10)
5. 用户没明确说在线/离线，默认在线(online_mode=true)
6. 用户没提到的布尔字段，按默认值填
7. 只输出JSON，不要额外解释
```

**User Prompt**：直接透传 `intent_text`。

**兜底**：LLM 返回非法 JSON 时，`recommend.py` 做容错解析（正则提取 JSON 块），仍失败则返回 `confidence=0`、`missing_fields` 含全部必填字段，前端引导用户手工补全。

### 5.3 `backend/chat/session.py`

CRUD 函数集，均为 async，走 `async_session`：

```python
async def create_session(intent_text, model_key) -> ChatSession
async def get_session(session_id) -> ChatSession | None
async def list_sessions(status, intent, page, page_size) -> tuple[list, int]
async def update_draft(session_id, draft_patch: dict) -> ChatSession  # 合并到 draft_request
async def mark_consumed(session_id, task_uuid) -> None
async def mark_error(session_id, error_message) -> None
async def delete_session(session_id) -> None
async def append_message(session_id, role, content, **kwargs) -> ChatMessage
async def list_messages(session_id) -> list[ChatMessage]
```

### 5.4 `apply` 如何复用现有 evaluate 逻辑

现有 `POST /evaluate`（`main.py:1244-1412`）是路由函数，内部逻辑：校验 dataset → 校验 agent → 建 `EvaluationTask` 行 → 启动后台任务。

阶段 1 的 `apply` 复用方式：**抽取 evaluate 路由内的核心逻辑为独立 async 函数**（如 `services/eval_task_service.py:create_and_start_eval_task(req: EvaluateRequest) -> str`），路由和 `apply` 都调它。这是最小重构，不改变现有 `POST /evaluate` 行为。

> 若不想动现有路由，备选方案：`apply` 内部用 `httpx.AsyncClient` 调本机 `POST /evaluate`（同进程 HTTP 回环）。代价是绕一圈 + token 校验。**推荐抽取函数方案**。

---

## 6. 前端设计

### 6.1 路由与入口

- `App.tsx` 的 `TabType` 新增 `'chat'`（`App.tsx:26-43`）
- 侧边栏"评估功能"分组新增"AI 配置"菜单项，`onClick` 设 `singletonPage='chat'`
- `App.tsx` 新增 `singletonPage === 'chat'` 渲染 `<ChatPage onApplyToEvalPage={...} />`
- 遵循 `onto-eval-ucd` skill 的卡片/标题/按钮规范

### 6.2 `ChatPage.tsx` 组件

```typescript
interface ChatPageProps {
  onApplyToEvalPage: (draft: EvaluateRequest, onlineMode: boolean) => void
  // 回填到表单：跳转 EvalPage 并预填
}
```

**内部状态**：
```typescript
const [sessions, setSessions] = useState<ChatSession[]>([])
const [currentSession, setCurrentSession] = useState<ChatSession | null>(null)
const [intentText, setIntentText] = useState('')
const [loading, setLoading] = useState(false)  // interpret 中
const [draft, setDraft] = useState<EvaluateRequest | null>(null)
const [missingFields, setMissingFields] = useState<string[]>([])
const [suggestions, setSuggestions] = useState<string[]>([])
```

**布局**（左中右三栏，遵循 UCD）：
- 左栏：会话列表（分页，可新建/删除）
- 中栏：意图输入框 + `EvalDraftPreviewCard`
- 右栏（可选）：建议提示

**交互流**：
1. 用户输入意图 → 点"解析配置"按钮 → `POST /chat/sessions` + `POST /chat/sessions/{id}/interpret`
2. 返回草稿 → 中栏展示 `EvalDraftPreviewCard`
3. 用户在卡片上改字段 → `PATCH /chat/sessions/{id}/draft`
4. 点"回填到表单"→ 调 `onApplyToEvalPage(draft, onlineMode)` → 跳转 `EvalPage`
5. 或点"直接启动"→ `POST /chat/sessions/{id}/apply` → 拿 `task_uuid` → 跳转结果监控

### 6.3 `EvalDraftPreviewCard.tsx` 组件

把 `EvaluateRequest` 草稿渲染成可读 + 可编辑卡片：

```typescript
interface EvalDraftPreviewCardProps {
  draft: EvaluateRequest
  missingFields: string[]
  suggestions: string[]
  onChange: (patch: Partial<EvaluateRequest>) => void   // 触发 PATCH draft
  onApplyToForm: () => void    // 回填到表单
  onApplyDirect: () => void    // 直接启动
}
```

**展示字段**（分组，遵循 UCD 卡片样式）：
- 基础：数据集 / 评估模式（在线·离线）/ Agent（在线才显示）
- 评估：LLM 模型 / 轮次 / 评估因子（chips）
- 维度：场景子步骤 / 过程一致性 / 运行稳定性（开关）
- 高级：BM25 / 文档库 / 本体包（折叠）

**可编辑**：每个字段就地编辑（下拉/输入/开关），改动触发 `onChange` → `PATCH /chat/sessions/{id}/draft`。

**缺失字段高亮**：`missingFields` 列表中的字段标红，提示用户补全。

**置信度**：右上角小标签显示 `confidence`（如"置信度 85%"），低置信度（<0.6）整体标黄提示"建议人工复核"。

### 6.4 回填到 `EvalPage` 的机制

现有 `EvalPage` props（`EvalPage.tsx:46,145`）：
```typescript
interface Props {
  mode: 'online' | 'offline'
  onEvaluationStart: ...
  onNavigateToResults: ...
  agentId?: string
}
```
**不支持预填**。需要新增一个 prop：

```typescript
interface Props {
  // ...现有
  initialDraft?: EvaluateRequest | null  // 新增：AI 草稿预填
}
```

`EvalPage` 内部 `useEffect` 监听 `initialDraft`，变化时把字段灌入各表单 state（`currentDatasetId`/`selectedAgentId`/`inferenceRounds`/`selectedMetrics` 等）。

**传递链路**（`App.tsx` 顶层）：
1. `App.tsx` 新增 state：`const [chatDraft, setChatDraft] = useState<EvaluateRequest | null>(null)`
2. `ChatPage` 的 `onApplyToEvalPage` → `setChatDraft(draft)` + 切换 `singletonPage` 到 `'online_eval'` 或 `'offline_eval'`（看 `draft.online_mode`）
3. `EvalPage` 渲染处（`App.tsx:925-942`）传入 `initialDraft={chatDraft}`
4. `EvalPage` 接收后灌入表单，用户可在表单上继续改，点"开始评估"走现有 `POST /evaluate`

这样 AI 草稿和手工表单完全解耦：AI 只负责产草稿，表单仍是唯一启动入口，AI 出错可随时人工改。

---

## 7. 实施任务清单

按依赖顺序执行，每项可独立验证。

### 7.1 后端

| # | 任务 | 产出 | 验收 |
|---|---|---|---|
| B1 | 新增 `ChatSession`/`ChatMessage` 模型 + alembic 029 迁移 | `database.py` 加两张表，`029_chat_session_tables.py` | `alembic upgrade head` 成功，PG 见表 |
| B2 | `chat/session.py` CRUD 函数 | `backend/chat/session.py` | 单元测试覆盖 create/get/list/update/delete |
| B3 | `chat/llm_router.py` 精简版 `chat_completion` | `backend/chat/llm_router.py` | mock `get_llm_client` 测试返回 (text, tokens) |
| B4 | `chat/recommend.py` `interpret_intent` + prompt | `backend/chat/recommend.py` | 给定意图文本，返回合法 JSON 草稿 |
| B5 | 抽取 `create_and_start_eval_task(req)` 为独立函数 | `backend/services/eval_task_service.py`，`main.py:1244` 路由改调它 | 现有 `POST /evaluate` 行为不变（回归测试） |
| B6 | 路由：`POST /chat/sessions` 等 5 条 | `main.py` 追加 | curl 建会话→interpret→draft→apply 全链路通 |
| B7 | `apply` 复用 `create_and_start_eval_task` | `main.py` 的 `/apply` 路由 | 返回真实 `task_uuid`，任务能跑 |

### 7.2 前端

| # | 任务 | 产出 | 验收 |
|---|---|---|---|
| F1 | `App.tsx` 加 `chat` TabType + 侧边栏入口 + `chatDraft` state | `App.tsx` | 点"AI 配置"进入 ChatPage |
| F2 | `ChatPage.tsx` 骨架（会话列表 + 意图输入 + 卡片区） | `frontend/src/components/ChatPage.tsx` | 页面渲染，遵循 UCD |
| F3 | `EvalDraftPreviewCard.tsx` | 同目录 | 草稿可读可编辑，缺失字段标红 |
| F4 | 对接 interpret/draft API | `ChatPage` 内 | 输入意图→出草稿→改字段→PATCH 通 |
| F5 | `EvalPage` 加 `initialDraft` prop + 回填 effect | `EvalPage.tsx:46,145` | 草稿灌入表单各字段 |
| F6 | `ChatPage` → `EvalPage` 回填链路 | `App.tsx` | 点"回填到表单"→跳转→表单预填 |

### 7.3 测试与联调

| # | 任务 | 验收 |
|---|---|---|
| T1 | 后端单元测试（session/llm_router/recommend） | 覆盖率达标 |
| T2 | 端到端：意图"评估 agent-001 在 ds_faq 上的忠实度，跑 3 轮" | 草稿正确，apply 生成 task_uuid，任务跑完出结果 |
| T3 | 端到端：意图模糊（"随便评一下"） | confidence 低，missing_fields 提示，不阻塞 |
| T4 | 回填链路：ChatPage 草稿 → EvalPage 表单 → 手工改 → 启动 | 字段全部正确灌入 |
| T5 | 大数据量：会话列表 1000 条分页查询 | <500ms |

---

## 8. 验收标准

1. 用户在 `ChatPage` 输入一句意图，3 秒内返回可读的 `EvalDraftPreviewCard`
2. 草稿字段可就地编辑，改动即时落库
3. 点"回填到表单"能正确预填 `EvalPage` 所有字段
4. 点"直接启动"能生成真实评测任务并跳转监控
5. 意图模糊时给出 `missing_fields` 提示，不报错
6. 现有 `EvalPage` 手工配置流程零影响（回归通过）
7. 1000 条会话分页查询 <500ms

---

## 9. 与阶段 2 的衔接

阶段 1 产出的所有模块都是阶段 2 的子集：

| 阶段 1 产出 | 阶段 2 如何扩展 |
|---|---|
| `ChatSession`/`ChatMessage` 表 | `ChatMessage` 加 tool_name/tool_args/tool_result 字段（已在 schema 预留） |
| `llm_router.chat_completion` | 扩展为 `chat_completion_with_tools(messages, tools)` |
| `recommend.py` 单轮 | `orchestrator.py` 多轮循环调 `llm_router` + `tools.py` |
| 5 条 API | 新增 `/stream` (SSE) 路由 |
| `ChatPage` + `EvalDraftPreviewCard` | 加 `ToolCallBubble` + `StreamingText` |
| `initialDraft` prop | 不变，阶段 2 的最终草稿同样走这个回填链路 |

阶段 1 不做任何会被阶段 2 推翻的设计，纯追加扩展。

---

## 10. 关键文件索引

| 用途 | 路径 |
|---|---|
| EvaluateRequest schema（草稿结构） | `backend/schemas.py:183-216` |
| LangfuseSourceConfig（离线来源） | `backend/schemas.py:170-181` |
| EvaluationTask 模型 | `backend/database.py:68-103` |
| LLMModel 模型 | `backend/database.py:655-670` |
| get_llm_client + 8 并发信号量 | `backend/evaluators/llm_client.py:65,84-110` |
| POST /evaluate 路由（apply 复用） | `backend/main.py:1244-1412` |
| EvalPage props | `frontend/src/components/EvalPage.tsx:46,145` |
| App.tsx TabType + singletonPage | `frontend/src/App.tsx:26-43,925-942` |
| alembic 最新版本 | `backend/alembic/versions/028_migrate_judging_mode_names.py` |
| 指标注册表 | `backend/evaluators/registry.py` |
| METRICS_CONFIG | `backend/config.py:64-71` |
