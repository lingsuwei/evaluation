# Onto Agent 调用指南

本文档介绍如何在 onto-eval 中调用 onto-agent 的接口。

## 1. 快速开始

### 1.1 配置环境变量

复制 `.env.example` 为 `.env` 并配置 onto-agent 参数：

```bash
# onto-agent 服务地址
ONTO_AGENT_BASE_URL=http://localhost:5055

# onto-agent API Token (从 onto-agent 的用户管理中获取)
ONTO_AGENT_API_TOKEN=your_onto_agent_token_here

# onto-agent 工作空间ID (可选)
ONTO_AGENT_WORKSPACE=

# onto-agent 请求超时时间(秒)
ONTO_AGENT_TIMEOUT=1800
```

### 1.2 基础调用

```python
from services import call_onto_agent
import asyncio

async def main():
    answer = await call_onto_agent(
        question="什么是本体工程?",
        base_url="http://localhost:5055",
        api_token="your-token"
    )
    print(answer)

asyncio.run(main())
```

## 2. 客户端类

### 2.1 OntoAgentClient

完整的 onto-agent 客户端，支持所有 API 操作：

```python
from services import OntoAgentClient, OntoAgentConfig

config = OntoAgentConfig(
    base_url="http://localhost:5055",
    api_token="your-token",
    workspace_id="optional-workspace-id",
    timeout=1800
)

client = OntoAgentClient(config)

# 同步调用获取完整回答
answer = await client.run_workflow_sync(
    request="什么是本体?",
    context={"domain": "制造业"}
)

# 流式调用（实时获取响应）
async for chunk in client.run_workflow(request="解释本体推理"):
    print(chunk, end="")
```

### 2.2 OntoAgentInferenceService

兼容 onto-eval 推理服务接口：

```python
from services import OntoAgentInferenceService

service = OntoAgentInferenceService()

# 标准推理调用
result = await service.call_inference(
    question="什么是AO工序?",
    config={},
    ground_truth="AO工序是..."
)

# result: {"model_answer": "...", "error": None}
```

## 3. 在评测中使用

### 3.1 评测单个样本

```python
from services import evaluate_with_onto_agent

dataset_item = {
    "question": "什么是本体?",
    "ground_truth": "本体是...",
    "context": "上下文信息"
}

result = await evaluate_with_onto_agent(
    dataset_item=dataset_item,
    onto_agent_config={
        "url": "http://localhost:5055",
        "api_token": "your-token"
    }
)

# result: {
#     "question": "什么是本体?",
#     "ground_truth": "本体是...",
#     "model_answer": "...",
#     "error": None
# }
```

### 3.2 批量评测

```python
from services import OntoAgentInferenceService

service = OntoAgentInferenceService()

dataset = [
    {"question": "问题1", "ground_truth": "答案1"},
    {"question": "问题2", "ground_truth": "答案2"},
]

results = []
for item in dataset:
    result = await service.call_inference(
        question=item["question"],
        config={},
        ground_truth=item["ground_truth"]
    )
    results.append({
        "question": item["question"],
        "ground_truth": item["ground_truth"],
        **result
    })
```

## 4. Session 管理

使用 Session 保持对话上下文：

```python
from services import OntoAgentClient, OntoAgentConfig

config = OntoAgentConfig(base_url="...", api_token="...")
client = OntoAgentClient(config)

# 创建 Session
session_id = await client.create_session(title="评测会话")

# 在同一 Session 中对话
answer1 = await client.run_workflow_sync(
    request="你好",
    session_id=session_id
)

answer2 = await client.run_workflow_sync(
    request="刚才我说了什么?",  # 能记住上文
    session_id=session_id
)

# 列出所有 Sessions
sessions = await client.list_sessions()

# 取消任务
success = await client.cancel_run(run_id="some-run-id")
```

## 5. API 参考

### OntoAgentClient 方法

| 方法 | 说明 |
|------|------|
| `authenticate()` | 验证 Token 有效性 |
| `create_session(title)` | 创建新 Session |
| `run_workflow(request, session_id, context)` | 流式调用工作流 |
| `run_workflow_sync(request, session_id, context)` | 同步调用工作流 |
| `get_run_status(run_id)` | 获取任务状态 |
| `list_sessions()` | 列出所有 Sessions |
| `cancel_run(run_id)` | 取消任务 |

### 便捷函数

| 函数 | 说明 |
|------|------|
| `call_onto_agent(question, ...)` | 直接调用获取回答 |
| `test_connection(config)` | 测试连接 |
| `call_onto_agent_inference(question, config, ground_truth)` | 推理服务调用 |
| `evaluate_with_onto_agent(dataset_item, config)` | 评测单个样本 |

## 6. 配置文件

### 从环境变量读取配置

```python
from config import get_onto_agent_config

config = get_onto_agent_config()
# {
#     "base_url": "http://localhost:5055",
#     "api_token": "...",
#     "workspace_id": None,
#     "timeout": 1800
# }
```

### 动态配置

```python
from services import OntoAgentClient, OntoAgentConfig

# 动态构建配置
config = OntoAgentConfig(
    base_url=os.getenv("ONTO_AGENT_BASE_URL"),
    api_token=os.getenv("ONTO_AGENT_API_TOKEN"),
    workspace_id=os.getenv("ONTO_AGENT_WORKSPACE"),
    timeout=int(os.getenv("ONTO_AGENT_TIMEOUT", "1800"))
)

client = OntoAgentClient(config)
```

## 7. 错误处理

```python
from services import OntoAgentClient, OntoAgentConfig

config = OntoAgentConfig(base_url="...", api_token="...")
client = OntoAgentClient(config)

# 检查连接
auth_result = await client.authenticate()
if "error" in auth_result:
    print(f"认证失败: {auth_result['error']}")
else:
    print(f"当前用户: {auth_result.get('user', {}).get('username')}")

# 调用处理错误
answer = await client.run_workflow_sync(request="...")
if answer.startswith("[ERROR]"):
    print(f"调用失败: {answer}")
else:
    print(f"回答: {answer}")
```

## 8. 示例代码

完整的示例代码位于：
- `backend/services/onto_agent_example.py` - 各种使用场景示例
- `backend/services/onto_agent_integration.py` - 与推理服务集成

运行示例：
```bash
cd backend
python -m services.onto_agent_example
```
