# onto-eval MCP 封装设计

> 日期：2026-08-10
> 状态：设计确认，待转实施计划
> 范围：P0 单题指标评测

## 1. 背景与目标

### 1.1 诉求

将 onto-eval 的指标评测能力封装为轻量接口，支持 Claude Code、Codex CLI、Trae CN、Cursor、OpenCode、Windsurf、Zed 等主流 AI 编码工具调用。核心目标：

- **调用体验最懒**：AI 工具原生工具调用，结构化入参出参，无 CLI 拼接摩擦
- **不因评测版本升级而受影响**：MCP 契约层与后端实现层解耦
- **轻量**：单目录，零部署负担
- **从简单开始**：P0 只做单题指标评测，其他能力按复杂度逐步叠加

### 1.2 路线选择

经对比 MCP server 与纯 skill+脚本（参考 `remote-agent-simtest` 实现）两条路线：

| 维度 | MCP server | 纯 skill+脚本 |
|---|---|---|
| 安装 | `pip install mcp` + 配 MCP 命令 | 零依赖放目录 |
| AI 调用体验 | 结构化工具调用，schema 一等公民 | AI 读 skill 后拼 CLI |
| 长文本参数 | JSON 无限制 | CLI 撞 shell 长度上限 |
| 调用可靠性 | 高（function call 原生） | 中（AI 可能拼错命令） |
| 权限摩擦 | 一次配置后免确认 | 每次执行 bash 默认确认 |
| 跨工具通用 | 协议统一，配置格式不同但同源 | 每家 skill/rules 格式完全不兼容 |

**选择 MCP server**。使用者反馈"最懒调用体验"优先于"最懒安装"，且 MCP 安装成本可控（仅多一个 `pip install mcp` + 一行配置）。

### 1.3 参考项目借鉴

`D:\work\HT集团\soft\remote-agent-simtest\remote-agent-simtest` 已验证 thin proxy + config.json + token 自动刷新模式。本设计吸收：

- config.json 集中管理 backend_url / token / 凭据 / 超时
- HTTP 客户端 401 自动刷新 token + 写回 config
- 标准库 urllib only（除 MCP SDK 外零依赖）

不吸收：

- 脚本形态（MCP 是 stdio 服务，不是 CLI 脚本）
- SSE 流式（P0 单题同步评测无需）

## 2. 架构

### 2.1 组件与数据流

```
┌──────────────────────────────────────────────────────────────┐
│ AI 工具层（任何支持 MCP 的工具）                               │
│   Claude Code  Cursor  Codex CLI  Trae CN  OpenCode          │
│   Windsurf  Zed  VS Code Copilot  Claude Desktop  ...        │
│        │                                                      │
│        │ MCP stdio                                            │
│        ▼                                                      │
│   ┌────────────────────────────────────────┐                 │
│   │ onto-eval-mcp (mcp/mcp_server.py)      │                 │
│   │ 工具: evaluate_answer / list_metrics   │                 │
│   │ 依赖: mcp SDK (唯一第三方依赖)         │                 │
│   └──────────────┬─────────────────────────┘                 │
│                  │ HTTP (urllib)                             │
│                  ▼                                           │
│   ┌────────────────────────────────────────┐                 │
│   │ onto-eval backend                      │                 │
│   │ POST /api/v1/evaluate/single (新增)    │                 │
│   │ GET  /api/v1/metrics          (扩展)   │                 │
│   │ → BaseMetric.evaluate()                │                 │
│   └────────────────────────────────────────┘                 │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 单题评测数据流

1. AI 工具调 `evaluate_answer(question, answer, metric, context?, ground_truth?, threshold?)`
2. MCP server 读 config.json → `BackendClient.post("/api/v1/evaluate/single", ...)`
3. 后端 `get_metric(name).evaluate()` → 返回 `{score, reason, passed, duration_ms, skipped}`
4. 401 时 BackendClient 自动刷新 token 重试
5. MCP server 透传结果给 AI 工具
6. AI 向用户呈现分数 / 通过状态 / 评估理由

### 2.3 稳定性边界

| 层 | 稳定性 | 内容 |
|---|---|---|
| **稳定契约层**（语义化版本锁定） | 破坏性变更需 major 版本 | MCP 工具 schema、`/api/v1/evaluate/single` 请求/响应 schema、`/metrics` 扩展字段、config.json 字段名 |
| **可变实现层**（自由演进） | 不影响 MCP 调用方 | `BaseMetric` 子类实现、LLM prompt、BM25 参数、综合分公式、Gate 机制、数据库结构、UI、评测模型升级 |

**核心保证**：`BaseMetric.evaluate(question, answer, context, ground_truth) → EvaluationResult(score, reason, duration_ms)` 是 onto-eval 的天然稳定契约，后端怎么升级都不会破坏。MCP 只是把它对外暴露。

## 3. 后端端点契约

### 3.1 POST `/api/v1/evaluate/single`

单题同步评测，无状态，不落库。

**请求**：
```json
{
  "question": "string",
  "answer": "string",
  "metric": "faithfulness",
  "context": "string|null",
  "ground_truth": "string|null",
  "threshold": "float|null",
  "model_key": "string|null"
}
```

**响应 200**：
```json
{
  "metric": "faithfulness",
  "score": 0.85,
  "passed": true,
  "threshold": 0.8,
  "reason": "回答完全由参考内容支持...",
  "duration_ms": 1200,
  "skipped": false,
  "eval_method": "llm"
}
```

**响应 400**（指标名无效）：
```json
{
  "error": "unknown_metric",
  "message": "Unknown metric: foo. Available: faithfulness, answer_relevancy, correctness",
  "available_metrics": ["faithfulness", "answer_relevancy", "correctness"]
}
```

**响应 503**（LLM 服务不可达）：
```json
{
  "error": "evaluator_error",
  "message": "LLM 调用超时 (180s)",
  "metric": "faithfulness"
}
```

**语义说明**：
- `score=null` + `skipped=true`：缺必要输入（如 faithfulness 缺 context），非错误，HTTP 200
- `score=0.0` + `skipped=false`：LLM 评估失败或答案确实得 0 分
- `passed = score >= threshold and not skipped`
- `eval_method`：P0 固定 `"llm"`

### 3.2 GET `/api/v1/metrics`（扩展现有 `/metrics`）

现有 `/metrics` 返回指标列表。扩展响应，每个指标加增量字段：

```json
{
  "metrics": [
    {
      "name": "faithfulness",
      "label": "忠诚度",
      "category": "quality",
      "requires": ["question", "answer", "context"],
      "optional": [],
      "default_threshold": 0.8,
      "eval_methods": ["llm"],
      "description": "评估回答是否忠实于上下文，检测幻觉"
    },
    {
      "name": "answer_relevancy",
      "label": "回答相关性",
      "category": "quality",
      "requires": ["question", "answer"],
      "optional": [],
      "default_threshold": 0.8,
      "eval_methods": ["llm"],
      "description": "评估回答是否紧扣问题"
    },
    {
      "name": "correctness",
      "label": "正确性",
      "category": "quality",
      "requires": ["question", "answer", "ground_truth"],
      "optional": [],
      "default_threshold": 0.8,
      "eval_methods": ["llm"],
      "description": "评估回答与标准答案的一致程度"
    }
  ]
}
```

新增字段为纯增量，前端忽略未知字段不受影响。需先检查现有 `/metrics` 返回结构，确保扩展兼容。

### 3.3 实现要点

- **同步端点**（`async def` 直接 `await metric.measure()`），不进任务队列
- 内部调 `get_metric(name).evaluate()` → `EvaluationResult`，再算 passed
- P0 faithfulness 强制走 LLM（不传 `use_bm25`）
- **不落库**：不写 `EvaluationResult` / `EvaluationRound` 表，避免污染任务级统计数据
- 超时：单题 LLM 评测最长 180s，HTTP 层 200s
- 无鉴权（与 onto-eval 现状一致）

## 4. MCP server 实现

### 4.1 文件结构

```
onto-eval/
└── mcp/                           ← 新增子目录，与 backend/ 同级
    ├── mcp_server.py              ← stdio MCP server 入口，~150 行
    ├── backend_client.py          ← urllib HTTP 客户端 + token 刷新，~120 行
    ├── config.py                  ← config.json 加载与校验，~40 行
    ├── config.json                ← 使用者配置（gitignore）
    ├── config.example.json        ← 模板
    ├── requirements.txt           ← 只有一行：mcp>=1.0
    ├── README.md                  ← 通用安装 + 各工具 MCP 配置速查
    └── tests/
        ├── test_backend_client.py
        └── test_mcp_server.py
```

### 4.2 config.json schema

```json
{
  "backend_url": "http://localhost:8000",
  "token": "",
  "username": "",
  "password": "",
  "token_expiry_skew_seconds": 60,
  "request_timeout": 200,
  "default_metric": "faithfulness",
  "default_threshold": null
}
```

- `backend_url`：onto-eval 后端地址，必填
- `token`/`username`/`password`：预留，onto-eval 当前无鉴权故留空
- `request_timeout`：HTTP 超时秒数，默认 200（覆盖单题 LLM 180s + 余量）
- `default_metric`/`default_threshold`：MCP 调用未指定时的默认值，可选

### 4.3 MCP 工具定义

#### 工具 1：`evaluate_answer`

单题同步评测。

**入参 schema**：
```json
{
  "question": "string",
  "answer": "string",
  "metric": "string",
  "context": "string|null",
  "ground_truth": "string|null",
  "threshold": "float|null"
}
```

- `question`/`answer`/`metric` 必填
- `context`：faithfulness 需要
- `ground_truth`：correctness 需要
- `threshold`：可选，默认用指标默认值

**出参**：后端 `/api/v1/evaluate/single` 响应透传。

**错误处理**：
- 后端不可达 → MCP 返回错误，提示检查 config.json 的 backend_url
- 指标名无效 → 透传 400 + available_metrics，AI 可自纠正调 list_metrics
- 缺必要输入 → 透传 skipped=true（非错误）

#### 工具 2：`list_metrics`

无入参，列出后端可用指标及元信息。出参为后端 `/api/v1/metrics` 响应透传。

### 4.4 backend_client.py 核心逻辑

吸收参考项目的 token 刷新模式：

- 标准库 `urllib` only（除 `mcp` SDK 外零依赖）
- `_request(method, path, body)`：统一请求方法
- 401 自动刷新 token（onto-eval 当前无鉴权，此路径预留）
- 错误透传给 MCP server

### 4.5 mcp_server.py 核心结构

- `list_tools()`：注册 `evaluate_answer` / `list_metrics` 两个工具
- `call_tool(name, arguments)`：分发到 BackendClient
- `stdio_server()`：stdio 传输
- config.json 热加载：每次工具调用 reload，改配置不用重启 server
- 错误信息对 AI 友好：后端 400 时透传 available_metrics 列表

### 4.6 跨工具配置通用性

一份 `mcp_server.py` 所有工具共用，零修改。配置层各工具有别，但都是 `command + args` 三元组：

| 工具 | 配置位置 | 格式 |
|---|---|---|
| Claude Code | `.claude/mcp.json` / 全局 | JSON |
| Cursor | `.cursor/mcp.json` / 设置 UI | JSON |
| Codex CLI | `~/.codex/config.toml` | TOML |
| Windsurf | 设置 UI | JSON |
| Trae CN | MCP 配置 UI | JSON |
| OpenCode | `opencode.json` / 全局 | JSON |
| Zed | `~/.config/zed/settings.json` | JSON |
| VS Code Copilot | `.vscode/mcp.json` | JSON |
| Claude Desktop | `claude_desktop_config.json` | JSON |

通用配置三元组：
```
command: python
args: ["/absolute/path/to/onto-eval/mcp/mcp_server.py"]
```

README 列常见工具配置速查，不在列表的工具查其 MCP 配置文档，原理相同。

## 5. 测试策略

### 5.1 后端测试（`backend/tests/test_evaluate_single.py`）

| 测试 | 覆盖点 |
|---|---|
| `test_evaluate_single_faithfulness` | LLM 评测 faithfulness，返回 score/reason/passed |
| `test_evaluate_single_correctness` | LLM 评测 correctness，ground_truth 缺失时 skipped=true |
| `test_evaluate_single_unknown_metric` | 400 + available_metrics 列表 |
| `test_evaluate_single_missing_input` | faithfulness 缺 context → skipped=true, score=null |
| `test_evaluate_single_custom_threshold` | threshold 覆盖默认值 |
| `test_metrics_extended_fields` | `/metrics` 响应含 requires/eval_methods/default_threshold |

LLM mock 策略：复用现有 `backend/tests/` 的 mock 模式。

### 5.2 MCP server 测试（`mcp/tests/`）

| 测试 | 覆盖点 |
|---|---|
| `test_backend_client_post` | HTTP POST 透传 + JSON 解析 |
| `test_backend_client_401_refresh` | 401 → 刷新 token → 重试成功 |
| `test_backend_client_connection_error` | 后端不可达 → 友好错误 |
| `test_mcp_evaluate_answer` | 端到端 MCP 工具调用（mock backend_client） |
| `test_mcp_list_metrics` | 端到端 MCP 工具调用 |
| `test_config_load` | config.json 加载与校验 |

### 5.3 端到端验证

启动 onto-eval 后端 → 启动 MCP server → 用 Claude Code 调 `evaluate_answer` 验证全链路通。

## 6. 性能考量

遵循 CLAUDE.md "大数据量下的性能表现"准则（300 题单轮、10题×10轮×4维度）。

P0 单题同步评测无批量场景，但为后续铺路的设计决策：

- **无状态可并发**：同步端点不落库，AI 工具可并行调多个 evaluate_answer
- **不落库避免写入瓶颈**：单题评测不写 EvaluationResult 表
- **LLM 速率限制是瓶颈**：现有 `_RATE_LIMIT_DELAY=2.0s`，短时间连调多题可能撞限制。P0 不处理，P1 批量时加并发控制

## 7. P0 边界（不做的事）

- 批量评测 / 异步任务（P1）
- 综合质量分 / 过程分 / Gate（P1）
- 运行时指标 inference_latency / tokens_total（P1）
- faithfulness BM25 / 文档库选项（P1）
- 落库持久化（P1，可选参数）
- 薄 skill 指引（P1，按需）
- HTTP/SSE 远程 MCP 传输（P1）
- 鉴权（onto-eval 当前无，config.json 预留）

## 8. 实施清单

### 后端

1. 新增 `POST /api/v1/evaluate/single` 端点（`backend/main.py`）
2. 扩展 `GET /metrics` 响应加 requires/eval_methods/default_threshold 等字段
3. 新增 `backend/tests/test_evaluate_single.py`

### MCP

4. 新增 `mcp/` 子目录
5. 实现 `mcp/config.py`（config.json 加载）
6. 实现 `mcp/backend_client.py`（urllib HTTP + token 刷新）
7. 实现 `mcp/mcp_server.py`（stdio MCP server，两个工具）
8. 新增 `mcp/config.example.json` + `mcp/requirements.txt` + `mcp/README.md`
9. 新增 `mcp/tests/test_backend_client.py` + `mcp/tests/test_mcp_server.py`
10. `.gitignore` 加 `mcp/config.json`

### 验证

11. 后端单测通过
12. MCP 单测通过
13. Claude Code 端到端调 `evaluate_answer` 验证全链路
