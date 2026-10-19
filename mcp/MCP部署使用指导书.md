# onto-eval MCP 部署使用指导书

将 onto-eval 的单题评测能力暴露给任何支持 MCP 的 AI 编程工具，让 AI 在对话中直接调用评测能力。

## 1. 概述

### 1.1 官方仓库

```bash
git clone git@gitcode.com:onto-huawei/onto-eval.git
```

### 1.2 能力简介

onto-eval MCP server 通过 stdio 传输协议与 AI 工具通信，暴露两个工具：

| 工具 | 说明 | 是否调 LLM |
|---|---|---|
| `list_metrics` | 列出后端可用评测指标及所需输入 | 否（免费）|
| `evaluate_answer` | 单题同步评测，返回分数 / 是否通过 / 评分理由 | 是（有费用）|

### 1.3 架构

```
AI 工具 (Claude Code / Codex / Trae / CodeBuddy / ...)
    │
    │ MCP stdio（子进程启动 python mcp_server.py）
    ▼
mcp/mcp_server.py          ← MCP 入口
    │
    │ HTTP（urllib，从 config.json 读 backend_url）
    ▼
onto-eval 后端 (FastAPI)
  GET  /metrics
  POST /api/v1/evaluate/single
```

**关键点**：AI 工具会自动把 MCP server 作为子进程拉起，不需要你手动守护运行。你只需告诉 AI 工具「怎么启动这个子进程」即可。

---

## 2. 前置条件

| 项 | 要求 | 检查命令 |
|---|---|---|
| Python | ≥ 3.11 | `python --version` |
| pip | 可正常安装包 | `pip --version` |
| onto-eval 后端 | 已运行且地址可达 | 见 §9.1 验证 |
| AI 工具 | 支持 MCP 协议 | — |

> MCP server 本身不需要数据库、不需要 LLM API Key——所有评测逻辑在后端完成。MCP server 只做 HTTP 转发。

---

## 3. 获取代码

```bash
git clone git@gitcode.com:onto-huawei/onto-eval.git
cd onto-eval/mcp
```

下文用 `$MCP_DIR` 代指 `onto-eval/mcp/` 目录的绝对路径。

| 系统 | 示例路径 |
|---|---|
| Windows | `D:/work/onto-eval/mcp` |
| macOS / Linux | `/home/you/onto-eval/mcp` |

> 各 AI 工具配置时需填入**绝对路径**，请把下文示例路径替换为你的实际路径。

---

## 4. 安装与配置

### 4.1 安装依赖

```bash
cd "$MCP_DIR"
pip install -r requirements.txt
```

唯一第三方依赖是 `mcp>=2.0`（见 `requirements.txt`），其余全用 Python 标准库，无额外负担。

### 4.2 配置 config.json

```bash
cp config.example.json config.json
```

编辑 `config.json`：

```json
{
  "backend_url": "http://your-backend-host:port",
  "token": "your-token-or-empty",
  "request_timeout": 200,
  "default_metric": "faithfulness",
  "default_threshold": null
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `backend_url` | ✓ | onto-eval 后端地址（含端口），如 `http://localhost:8000` 或 `http://1.2.3.4:45500` |
| `token` | 否 | 鉴权 token，须与后端 `EVAL_SINGLE_TOKEN` 环境变量一致；后端未设则留空 |
| `request_timeout` | 否 | HTTP 超时秒数，默认 200（评测调 LLM 较慢，建议 ≥ 200）|
| `default_metric` | 否 | 未指定指标时的默认值 |
| `default_threshold` | 否 | 未指定阈值时的默认值 |

> **改 config.json 后无需重启 MCP server**——每次工具调用都会重新读取配置（热加载）。

### 4.3 验证 MCP server 可启动

```bash
python "$MCP_DIR/mcp_server.py"
```

启动后**无输出、进程挂住**等待 stdin 输入 = 正常（stdio MCP server 的行为）。`Ctrl+C` 退出即可，AI 工具会自动管理它的生命周期。

- 报 `ModuleNotFoundError: No module named 'mcp'` → 回 §4.1 装依赖
- 报 `FileNotFoundError: config.json` → 回 §4.2 建配置

---

## 5. AI 工具接入

### 5.1 通用配置原理

所有 AI 工具的 MCP 配置本质都是同一个三元组：

```
command: python
args:    ["/absolute/path/to/onto-eval/mcp/mcp_server.py"]
```

不同工具只是配置文件格式和位置不同。下文按工具逐个说明。

> **路径注意**：
> - 必须用**绝对路径**
> - Windows 路径用正斜杠 `/`，不要用反斜杠 `\`（TOML/JSON 中 `\` 是转义符）
> - 如果 `python` 不在 PATH，用完整路径，如 `C:/Python311/python.exe`

---

### 5.2 Claude Code

**配置文件**：项目根 `.claude/mcp.json`（项目级）或 `~/.claude.json` 的 `mcpServers` 字段（全局）

```json
{
  "mcpServers": {
    "onto-eval": {
      "type": "stdio",
      "command": "python",
      "args": ["/absolute/path/to/onto-eval/mcp/mcp_server.py"]
    }
  }
}
```

也可用 CLI 一键添加：

```bash
claude mcp add onto-eval python /absolute/path/to/onto-eval/mcp/mcp_server.py
```

重启 Claude Code 后，工具名前缀为 `mcp__onto-eval__`（如 `mcp__onto-eval__list_metrics`）。

---

### 5.3 Codex CLI

**配置文件**：`~/.codex/config.toml`

| 系统 | 路径 |
|---|---|
| Windows | `C:\Users\你的用户名\.codex\config.toml` |
| macOS / Linux | `~/.codex/config.toml` |

```toml
[mcp_servers.onto-eval]
command = "python"
args = ["/absolute/path/to/onto-eval/mcp/mcp_server.py"]
```

> TOML 中字符串无需引号转义，但路径仍建议用正斜杠。

---

### 5.4 Trae CN

**配置方式**：Trae 设置界面 → MCP 配置 → 添加新 MCP server

- Name: `onto-eval`
- Command: `python`
- Args: `/absolute/path/to/onto-eval/mcp/mcp_server.py`

或编辑 Trae 的 MCP 配置文件（JSON 格式，与 Claude Code 相同）：

```json
{
  "mcpServers": {
    "onto-eval": {
      "command": "python",
      "args": ["/absolute/path/to/onto-eval/mcp/mcp_server.py"]
    }
  }
}
```

---

### 5.5 腾讯 CodeBuddy

**配置方式**：CodeBuddy 设置 → MCP Servers → 新增

CodeBuddy 作为 IDE 插件运行，MCP 配置采用标准 JSON 格式，与 VS Code 一致：

```json
{
  "mcpServers": {
    "onto-eval": {
      "command": "python",
      "args": ["/absolute/path/to/onto-eval/mcp/mcp_server.py"]
    }
  }
}
```

> CodeBuddy 近期迭代较快，如配置入口有变动，核心仍是 `command + args` 三元组。详见 CodeBuddy 官方文档。

---

### 5.6 Cursor

**配置文件**：项目根 `.cursor/mcp.json` 或设置 UI → MCP

```json
{
  "mcpServers": {
    "onto-eval": {
      "command": "python",
      "args": ["/absolute/path/to/onto-eval/mcp/mcp_server.py"]
    }
  }
}
```

---

### 5.7 Windsurf

**配置方式**：设置 UI → MCP Servers → 添加新 server

- Name: `onto-eval`
- Command: `python`
- Args: `/absolute/path/to/onto-eval/mcp/mcp_server.py`

---

### 5.8 VS Code Copilot

**配置文件**：项目根 `.vscode/mcp.json`

```json
{
  "mcpServers": {
    "onto-eval": {
      "command": "python",
      "args": ["/absolute/path/to/onto-eval/mcp/mcp_server.py"]
    }
  }
}
```

---

### 5.9 Claude Desktop

**配置文件**：

| 系统 | 路径 |
|---|---|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "onto-eval": {
      "command": "python",
      "args": ["/absolute/path/to/onto-eval/mcp/mcp_server.py"]
    }
  }
}
```

---

### 5.10 Zed

**配置文件**：`~/.config/zed/settings.json`

```json
{
  "mcp_servers": {
    "onto-eval": {
      "command": "python",
      "args": ["/absolute/path/to/onto-eval/mcp/mcp_server.py"]
    }
  }
}
```

---

### 5.11 OpenCode

**配置文件**：`opencode.json` 或全局配置（JSON 格式，同 Claude Code）

```json
{
  "mcpServers": {
    "onto-eval": {
      "command": "python",
      "args": ["/absolute/path/to/onto-eval/mcp/mcp_server.py"]
    }
  }
}
```

---

### 5.12 不在列表的工具

查该工具的 MCP 配置文档，填入同一个三元组：

```
command: python
args:    ["/absolute/path/to/onto-eval/mcp/mcp_server.py"]
```

**排查要点**：
- 配置格式是 JSON 还是 TOML？
- 配置文件在项目级还是用户级？
- server 名称是否允许连字符？（`onto-eval` 如不支持，改成 `onto_eval`）

---

## 6. 使用方法

配置完成后，在 AI 工具对话框中用自然语言描述需求即可，AI 会自动调用 MCP 工具。

### 6.1 list_metrics — 列出可用指标

> 帮我列出 onto-eval 可用的评测指标

返回所有指标的 key、所需输入、类别。**不调用 LLM，免费**。

### 6.2 evaluate_answer — 评测一道题

#### 必填参数

| 参数 | 说明 |
|---|---|
| `question` | 问题内容 |
| `answer` | 待评估的回答 |
| `metric` | 指标名：`faithfulness` / `answer_relevancy` / `correctness` |

#### 可选参数

| 参数 | 什么时候要 | 默认值 |
|---|---|---|
| `context` | `faithfulness` 必填，否则 score=null 跳过 | — |
| `ground_truth` | `correctness` 必填，否则跳过 | — |
| `threshold` | 自定义通过线，不填用指标默认 0.8 | 0.8 |

#### 三个指标怎么选

| 场景 | 指标 | 需要 |
|---|---|---|
| 回答是否跑题、是否紧扣问题 | `answer_relevancy` | question + answer |
| 回答有没有编造、是否忠于参考资料 | `faithfulness` | question + answer + **context** |
| 回答对不对、跟标准答案一不一致 | `correctness` | question + answer + **ground_truth** |

#### 调用示例

自然语言（推荐）：

> 用 correctness 评测：问题是「什么是知识图谱？」，回答是「知识图谱是结构化的知识表示形式」，标准答案是「知识图谱是用图结构组织实体和关系的知识库」。

AI 会自动调用 `evaluate_answer` 工具，参数：
- `metric`: `correctness`
- `question`: 什么是知识图谱？
- `answer`: 知识图谱是结构化的知识表示形式
- `ground_truth`: 知识图谱是用图结构组织实体和关系的知识库

#### 返回结果

```json
{
  "metric": "correctness",
  "score": 0.92,           // 0-1，越高越好
  "passed": true,          // 是否达到 threshold
  "threshold": 0.8,        // 通过线
  "reason": "...",          // LLM 评分理由
  "duration_ms": 21052,    // 评测耗时
  "skipped": false,        // true=缺输入被跳过，score=null
  "eval_method": "llm"
}
```

- 看 `score` + `passed` 判断质量
- `skipped: true` 说明漏了必填输入（如 faithfulness 没给 context），不是错误但没评出分

---

## 7. Token 鉴权（可选）

后端 `/api/v1/evaluate/single` 会调用 LLM 产生费用。**如后端部署在公网，强烈建议开启 token 校验**。

### 7.1 后端设置环境变量

在后端 `.env` 或部署环境中：

```bash
EVAL_SINGLE_TOKEN="your-secret-token"
```

### 7.2 MCP config.json 填入相同 token

```json
{
  "backend_url": "http://your-backend:port",
  "token": "your-secret-token"
}
```

后端未设置 `EVAL_SINGLE_TOKEN` 时端点开放（本地开发无负担）。生产环境务必设置。

---

## 8. 验证与测试

### 8.1 连通性测试（curl）

配置 AI 工具前，先确认后端两个端点可达：

```bash
# list_metrics 用的路径（应返回 JSON 指标列表）
curl -sS http://your-backend:port/metrics

# evaluate_answer 用的路径（空 body 应返回 422 参数校验失败）
curl -sS -X POST http://your-backend:port/api/v1/evaluate/single \
  -H "Content-Type: application/json" \
  -H "X-Eval-Token: your-token" \
  -d '{}'
```

| 期望结果 | 说明 |
|---|---|
| `/metrics` 返回 JSON | list_metrics 工具可用 |
| POST 空 body 返回 422 | evaluate_answer 路由存在且 token 通过 |
| POST 空 body 返回 401 | token 不匹配，检查 config.json 的 `token` |
| 返回 HTML | Nginx 未反代该路径，联系后端运维 |

### 8.2 AI 工具内验证

在 AI 工具对话框输入：

> 帮我调用 onto-eval 的 list_metrics 工具，列出可用指标

如返回指标列表（faithfulness / answer_relevancy / correctness 等），部署成功。

### 8.3 端到端验证脚本

仓库内置 E2E 验证脚本，走完整链路（MCP → HTTP → 后端 → LLM）：

```bash
cd "$MCP_DIR"
python e2e_verify.py
```

脚本会依次验证：
1. `list_metrics` 返回指标列表
2. `evaluate_answer`（answer_relevancy，真实 LLM 调用，3-30 秒）
3. `evaluate_answer`（faithfulness，真实 LLM）

默认指向 `http://localhost:3001`。如需指向其他后端，改 `e2e_verify.py` 顶部的 `TEST_CONFIG`。

---

## 9. 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| `ModuleNotFoundError: No module named 'mcp'` | 依赖未装 | `pip install -r requirements.txt` |
| `FileNotFoundError: config.json` | 配置文件缺失 | `cp config.example.json config.json` 并编辑 |
| `Expecting value: line 1 column 1` | 后端返回了 HTML 而非 JSON，通常是 Nginx 未反代 `/metrics` 路径 | 联系后端运维加 `location = /metrics` 反代规则 |
| `HTTP 401: invalid_token` | token 不匹配 | config.json 的 `token` 须与后端 `EVAL_SINGLE_TOKEN` 一致 |
| `HTTP 404` on evaluate_answer | Nginx 剥了 `/api` 前缀 | 后端路由是 `/api/v1/evaluate/single`，Nginx 需对 `/api/v1/` 原样转发（proxy_pass 不带尾斜杠）|
| `HTTP 400: unknown_metric` | metric 名拼写错 | 只允许 `faithfulness` / `answer_relevancy` / `correctness` |
| `skipped: true` | 缺必填输入 | faithfulness 需 context，correctness 需 ground_truth |
| 评测超时 | LLM 响应慢 | 增大 config.json 的 `request_timeout`（如 600）|
| AI 工具找不到工具 | 配置文件路径或格式错 | 确认绝对路径、JSON/TOML 语法、重启 AI 工具 |

---

## 10. 目录结构速查

```
onto-eval/mcp/
├── mcp_server.py          ← MCP server 入口（AI 工具启动这个）
├── backend_client.py      ← 后端 HTTP 客户端（urllib，零额外依赖）
├── config.py              ← config.json 加载与校验（热加载）
├── config.json            ← 实际配置（自行创建，不提交 git）
├── config.example.json    ← 配置模板
├── requirements.txt       ← 依赖清单（mcp>=2.0）
├── e2e_verify.py         ← 端到端验证脚本
├── tests/                 ← 单元测试
└── README.md              ← 简版速查
```

---

## 11. 快速上手清单

部署只需 3 步：

1. **装依赖 + 建配置**
   ```bash
   cd onto-eval/mcp
   pip install -r requirements.txt
   cp config.example.json config.json
   # 编辑 config.json 填入 backend_url
   ```

2. **配置 AI 工具**（见 §5，选你的工具）—— 填入 `python` + `mcp_server.py 绝对路径`

3. **验证** —— 在 AI 工具中说「列出 onto-eval 可用指标」，返回指标列表即成功
