# onto-eval MCP server

将 onto-eval 的单题指标评测能力暴露给任何支持 MCP 的 AI 工具（Claude Code、Cursor、Codex CLI、Trae CN、CodeBuddy、OpenCode、Windsurf、Zed、VS Code Copilot 等）。

> **完整部署使用指导书**：见同目录 [`MCP部署使用指导书.md`](./MCP部署使用指导书.md)（含官方仓库地址、前置条件、验证步骤、使用示例、常见问题排查）。
>
> 本 README 为简版速查，快速接入可直接看下文。

## 官方仓库

```bash
git clone git@gitcode.com:onto-huawei/onto-eval.git
```

## 安装

```bash
# 1. 进入 mcp 目录
cd onto-eval/mcp

# 2. 安装唯一依赖
pip install -r requirements.txt

# 3. 复制配置模板
cp config.example.json config.json

# 4. 编辑 config.json，填入 onto-eval 后端地址
#    backend_url 改为实际地址，如 http://localhost:8000
```

## 可用工具

| 工具 | 说明 |
|---|---|
| `evaluate_answer` | 单题评测。入参 question/answer/metric/context?/ground_truth?/threshold? |
| `list_metrics` | 列出可用指标及所需输入 |

## AI 工具配置

所有工具的 MCP 配置都是同一个 `command + args` 三元组：

```
command: python
args: ["/absolute/path/to/onto-eval/mcp/mcp_server.py"]
```

### 常见工具配置速查

**Claude Code**（`.claude/mcp.json`）：
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

**Cursor**（`.cursor/mcp.json` 或设置 UI）：
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

**Codex CLI**（`~/.codex/config.toml`）：
```toml
[mcp_servers.onto-eval]
command = "python"
args = ["/absolute/path/to/onto-eval/mcp/mcp_server.py"]
```

**腾讯 CodeBuddy**（设置 → MCP Servers，JSON 格式同 VS Code）：
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

**Windsurf**（设置 UI → MCP Servers）：添加新 server，填 command 和 args。

**Trae CN**（MCP 配置 UI）：添加新 MCP server，填 command 和 args。

**OpenCode**（`opencode.json` 或全局配置）：JSON 格式，同 Claude Code。

**Zed**（`~/.config/zed/settings.json`）：JSON 格式。

**VS Code Copilot**（`.vscode/mcp.json`）：JSON 格式，同 Claude Code。

**Claude Desktop**（`claude_desktop_config.json`）：JSON 格式，同 Claude Code。

> 不在列表的工具：查其 MCP 配置文档，原理相同——填 `python /path/to/mcp_server.py`。

## 配置说明

`config.json` 字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `backend_url` | 是 | onto-eval 后端地址 |
| `token` | 否 | 鉴权 token，通过 `X-Eval-Token` header 发送给后端。后端设置 `EVAL_SINGLE_TOKEN` 环境变量时必须匹配，未设则开放 |
| `request_timeout` | 否 | HTTP 超时秒数，默认 200 |
| `default_metric` | 否 | 未指定指标时的默认值 |
| `default_threshold` | 否 | 未指定阈值时的默认值 |

修改 config.json 后无需重启 MCP server，下次工具调用自动热加载。

### Token 鉴权（可选）

后端 `/api/v1/evaluate/single` 端点会调用 LLM（产生费用）。如后端部署在公网，建议设置 `EVAL_SINGLE_TOKEN` 环境变量开启 token 校验：

```bash
# 后端启动时设置环境变量
export EVAL_SINGLE_TOKEN="your-secret-token"
```

然后在 MCP 的 `config.json` 中填入相同的 token：

```json
{
  "backend_url": "http://your-backend:8000",
  "token": "your-secret-token"
}
```

后端未设置 `EVAL_SINGLE_TOKEN` 时端点开放（本地开发无负担）。