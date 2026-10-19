# LLM Evaluation System

Multi-model, multi-metric async evaluation system with CoT streaming.

## Quick Start

### 本地开发启动

#### Backend

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

#### Frontend

```bash
cd frontend
npm install
npm run dev
```

访问地址: http://localhost:3000

---

### 容器启动 (Docker Compose)

#### 前置要求

- Docker
- Docker Compose

#### 启动步骤

1. 配置环境变量（可选）

创建 `.env` 文件并配置以下变量：

```bash
GLM_API_KEY=your_api_key_here
GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
GLM_MODEL=glm-4.7
ENABLED_METRICS=faithfulness,answer_relevancy,correctness
```

2. 编译镜像

```bash
docker-compose build --no-cache
```

3. 启动服务

```bash
docker-compose up -d
```

4. 查看日志

```bash
docker-compose logs -f
```

#### 常用操作

**停止服务**

```bash
docker-compose stop
```

**重启服务**

```bash
docker-compose restart
```

**停止并删除容器**

```bash
docker-compose down
```

**停止并删除容器及数据卷**

```bash
docker-compose down -v
```

访问地址: http://localhost:45000

#### 服务端口

- `45000`: 前端 (Nginx)
- `8000`: 后端 API (FastAPI)

#### 数据持久化

- 数据库: `./backend/data/evaluation.db`
- 日志: `./backend/logs/`

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/upload` | POST | Upload JSON dataset |
| `/evaluate` | POST | Start async evaluation |
| `/result/{task_uuid}` | GET | Poll for results |
| `/stream/{task_uuid}` | GET | SSE stream for real-time CoT |
| `/tasks` | GET | List historical tasks |
| `/health` | GET | Health check |

---

### POST `/upload`

上传并验证 JSON 数据集。

**请求参数：**
- `file` (multipart/form-data): JSON 文件

**请求体格式：**
```json
[
  {
    "question": "问题内容",
    "context": "可选的上下文",
    "ground_truth": "可选的标准答案",
    "model_answer": "模型回答"
  }
]
```

**响应：**
```json
{
  "dataset_id": "ds_xxxxxxxx",
  "item_count": 10
}
```

**错误码：**
- `400`: JSON 格式无效或数据验证失败

---

### POST `/evaluate`

启动异步评估任务。

**请求体：**
```json
{
  "dataset_id": "ds_xxxxxxxx",
  "model_name": "default"  // 可选，默认为 "default"
}
```

**响应 (202 Accepted)：**
```json
{
  "task_uuid": "task_xxxxxxxx"
}
```

**错误码：**
- `404`: 数据集不存在

---

### GET `/result/{task_uuid}`

轮询获取评估结果。

**路径参数：**
- `task_uuid`: 任务 ID

**响应（处理中）：**
```json
{
  "status": "pending" | "processing",
  "progress": "5/10"
}
```

**响应（完成）：**
```json
{
  "status": "completed",
  "summary": {
    "radar_data": {
      "model_name": {
        "faithfulness": 0.85,
        "answer_relevancy": 0.92
      }
    }
  },
  "details": [
    {
      "question_id": "q_001",
      "model": "default",
      "metrics": [
        {
          "name": "faithfulness",
          "score": 0.85,
          "threshold": 0.8,
          "passed": true,
          "duration_ms": 1200,
          "cot": "思维链推理过程..."
        }
      ]
    }
  ]
}
```

**响应（失败）：**
```json
{
  "status": "failed",
  "error": "错误信息"
}
```

**错误码：**
- `404`: 任务不存在

---

### GET `/stream/{task_uuid}`

SSE 实时流式获取评估进度和 CoT 推理过程。

**路径参数：**
- `task_uuid`: 任务 ID

**SSE 事件类型：**
- `cot_chunk`: CoT 推理片段
- `metric_complete`: 单个指标评估完成
- `task_complete`: 任务完成
- `task_error`: 任务出错
- `ping`: 心跳保活

**事件数据示例：**
```
event: cot_chunk
data: {"content": "正在分析答案的忠实度..."}

event: metric_complete
data: {"question_id": "q_001", "metric": "faithfulness", "score": 0.85}

event: task_complete
data: {"task_id": "task_xxxxxxxx"}
```

---

### GET `/tasks`

分页获取历史任务列表。

**查询参数：**
- `page`: 页码，默认 1
- `limit`: 每页数量，默认 20

**响应：**
```json
{
  "tasks": [
    {
      "id": "task_xxxxxxxx",
      "dataset_id": "ds_xxxxxxxx",
      "status": "completed",
      "created_at": "2025-01-01T12:00:00",
      "completed_at": "2025-01-01T12:05:00"
    }
  ],
  "page": 1,
  "limit": 20
}
```

---

### GET `/health`

健康检查接口。

**响应：**
```json
{
  "status": "ok"
}

}
```

---

## Dataset Format

数据集为 JSON 数组，每个元素包含：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `question` | string | ✅ | 问题内容 |
| `context` | string | ❌ | 上下文信息 |
| `ground_truth` | string | ❌ | 标准答案 |
| `model_answer` | string | ✅ | 模型回答 |

**示例：**
```json
[
  {
    "question": "What is machine learning?",
    "context": "Machine learning is a subset of artificial intelligence...",
    "ground_truth": "Machine learning is a method of data analysis that automates analytical model building.",
    "model_answer": "Machine learning is a type of AI that allows computers to learn from data."
  }
]
```

## Available Metrics

- `faithfulness` - Checks if answer is faithful to context
- `answer_relevancy` - Checks if answer is relevant to question
- `correctness` - Compares against expected answer

## Architecture

```
Frontend (React) <---> FastAPI Backend <---> SQLite DB
                           |
                      Async Tasks
                           |
                    Metric Evaluators
```

## Production Deployment

```bash
# Start API
uvicorn main:app --host 0.0.0.0 --port 8000
```
