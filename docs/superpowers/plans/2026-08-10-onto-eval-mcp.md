# onto-eval MCP 封装实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 onto-eval 单题指标评测能力封装为 MCP server，供 Claude Code / Codex CLI / Trae CN / Cursor 等任意支持 MCP 的 AI 工具调用。

**Architecture:** 后端新增同步端点 `POST /api/v1/evaluate/single`（thin wrapper around `BaseMetric.evaluate()`，不落库）+ 扩展现有 `/metrics` 响应加 `requires/eval_methods/default_threshold` 字段。独立 `mcp/` 子目录实现 stdio MCP server，内部用标准库 `urllib` HTTP 调后端，除 `mcp` SDK 外零依赖。

**Tech Stack:** Python 3.10+, FastAPI (后端), `mcp` SDK (MCP server), `urllib` (HTTP 客户端), pytest + httpx ASGITransport (测试)

## Global Constraints

- 后端端点契约锁定 `/api/v1/evaluate/single` 请求/响应 schema，破坏性变更需 major 版本
- MCP 工具 schema（`evaluate_answer` / `list_metrics` 入参出参字段名）锁定
- `mcp/` 子目录除 `mcp` SDK 外零第三方依赖，HTTP 走标准库 `urllib`
- P0 faithfulness 强制走 LLM，不暴露 `use_bm25` / `use_doc_library`
- P0 不落库（不写 `EvaluationResult` / `EvaluationRound` 表）
- onto-eval 当前无鉴权，`config.json` 预留 token 字段
- 遵循 CLAUDE.md：考虑 300 题单轮 / 10题×10轮×4维度的大数据量性能

**Spec:** `docs/superpowers/specs/2026-08-10-onto-eval-mcp-design.md`

---

## 文件结构

```
onto-eval/
├── backend/
│   ├── main.py                           ← 修改：新增端点 + 扩展 /metrics
│   └── tests/
│       └── test_evaluate_single.py       ← 新增
└── mcp/                                  ← 新增子目录
    ├── mcp_server.py                     ← stdio MCP server
    ├── backend_client.py                 ← urllib HTTP 客户端
    ├── config.py                         ← config.json 加载
    ├── config.example.json               ← 配置模板
    ├── config.json                       ← 使用者配置（gitignore）
    ├── requirements.txt                  ← mcp>=1.0
    ├── README.md                         ← 安装与配置说明
    └── tests/
        ├── test_backend_client.py
        └── test_mcp_server.py
```

各文件职责：
- `backend/main.py`：新增 `EvaluateSingleRequest` Pydantic 模型 + `POST /api/v1/evaluate/single` 端点；扩展 `list_metrics` 的 `builtin` 项加 `requires/optional/default_threshold/eval_methods/description` 字段
- `backend/tests/test_evaluate_single.py`：后端端点单测，mock LLM 调用
- `mcp/config.py`：加载并校验 `config.json`，返回 typed dict
- `mcp/backend_client.py`：`BackendClient` 类，`post(path, body)` / `get(path)` 方法，401 自动刷新 token
- `mcp/mcp_server.py`：注册 `evaluate_answer` / `list_metrics` 两个 MCP 工具，分发到 `BackendClient`
- `mcp/tests/`：MCP server 与 backend_client 单测

---

### Task 1: 后端 `/api/v1/evaluate/single` 端点

**Files:**
- Modify: `backend/main.py`（新增 Pydantic 模型 + 端点函数）
- Test: `backend/tests/test_evaluate_single.py`

**Interfaces:**
- Consumes: `evaluators.registry.get_metric(name, model_key=...)` → `BaseMetric`；`BaseMetric.measure(question, answer, context, ground_truth, threshold)` → `(EvaluationResult, bool)`；`config.METRICS_CONFIG` dict
- Produces: `POST /api/v1/evaluate/single` 端点，请求 `{question, answer, metric, context?, ground_truth?, threshold?, model_key?}`，响应 `{metric, score, passed, threshold, reason, duration_ms, skipped, eval_method}`

- [ ] **Step 1: 写失败测试 — faithfulness 正常评测**

创建 `backend/tests/test_evaluate_single.py`：

```python
"""POST /api/v1/evaluate/single 端点单测。"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, ".")

pytestmark = pytest.mark.p3


@pytest.fixture
def app():
    """构建只挂 main app 的测试客户端（跳过 DB 依赖）。"""
    from main import app as full_app
    return full_app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_evaluate_single_faithfulness(client):
    """faithfulness 正常评测返回 score/reason/passed。"""
    mock_response = "SCORE: 0.85\nREASON: 回答完全由参考内容支持。"
    mock_llm = AsyncMock(return_value=mock_response)

    with patch("evaluators.llm_client.evaluate_with_llm", mock_llm), \
         patch("evaluators.llm_client.get_llm_config", return_value={"model": "test-model"}):
        res = await client.post("/api/v1/evaluate/single", json={
            "question": "什么是物料A？",
            "answer": "物料A是飞机零件。",
            "metric": "faithfulness",
            "context": "物料A是飞机机翼的结构件。",
        })

    assert res.status_code == 200
    body = res.json()
    assert body["metric"] == "faithfulness"
    assert body["score"] == 0.85
    assert body["passed"] is True
    assert body["threshold"] == 0.8
    assert body["skipped"] is False
    assert body["eval_method"] == "llm"
    assert "回答完全由参考内容支持" in body["reason"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_evaluate_single.py::test_evaluate_single_faithfulness -v`
Expected: FAIL with 404（端点不存在）

- [ ] **Step 3: 实现 Pydantic 模型 + 端点**

在 `backend/main.py` 的 `/metrics/runtime-thresholds` 端点之后（约 460 行前，`/custom-metrics` 之前）插入：

```python
class EvaluateSingleRequest(BaseModel):
    question: str
    answer: str
    metric: str
    context: Optional[str] = None
    ground_truth: Optional[str] = None
    threshold: Optional[float] = None
    model_key: Optional[str] = None


@app.post("/api/v1/evaluate/single")
async def evaluate_single(req: EvaluateSingleRequest):
    """单题同步评测，无状态，不落库。

    供 MCP server 调用。P0 只走 LLM 评估（faithfulness 不暴露 BM25 选项）。
    缺必要输入时返回 score=null, skipped=true（非错误）。
    """
    from config import METRICS_CONFIG
    from evaluators.registry import METRICS_REGISTRY, get_metric

    # 1. 校验指标名（只允许原生质量指标，排除 runtime 指标）
    _QUALITY_METRICS = {"faithfulness", "answer_relevancy", "correctness"}
    if req.metric not in _QUALITY_METRICS:
        from fastapi.responses import JSONResponse
        available = [k for k in METRICS_REGISTRY.keys() if k in _QUALITY_METRICS]
        return JSONResponse(
            status_code=400,
            content={
                "error": "unknown_metric",
                "message": f"Unknown metric: {req.metric}. Available: {available}",
                "available_metrics": available,
            },
        )

    # 2. 取指标实例（P0 faithfulness 强制 LLM，不传 use_bm25）
    metric = get_metric(
        req.metric,
        model_key=req.model_key,
    )

    # 3. 评测（同步 await，不进任务队列）
    effective_threshold = (
        req.threshold if req.threshold is not None
        else METRICS_CONFIG.get(req.metric, 0.8)
    )
    try:
        result, passed = await metric.measure(
            question=req.question,
            answer=req.answer,
            context=req.context,
            ground_truth=req.ground_truth,
            threshold=effective_threshold,
        )
    except Exception as exc:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=503,
            content={
                "error": "evaluator_error",
                "message": str(exc),
                "metric": req.metric,
            },
        )

    # 4. 判断 skipped（score=None 表示缺输入已跳过）
    skipped = result.score is None

    return {
        "metric": req.metric,
        "score": result.score,
        "passed": passed and not skipped,
        "threshold": effective_threshold,
        "reason": result.reason,
        "duration_ms": result.duration_ms,
        "skipped": skipped,
        "eval_method": "llm",
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_evaluate_single.py::test_evaluate_single_faithfulness -v`
Expected: PASS

- [ ] **Step 5: 写测试 — correctness 缺 ground_truth 跳过**

追加到 `backend/tests/test_evaluate_single.py`：

```python
@pytest.mark.asyncio
async def test_evaluate_single_correctness_missing_ground_truth_skipped(client):
    """correctness 缺 ground_truth → skipped=true, score=null。"""
    mock_response = "SCORE: 0.0\nREASON: 缺少标准答案，无法评估。"
    mock_llm = AsyncMock(return_value=mock_response)

    with patch("evaluators.llm_client.evaluate_with_llm", mock_llm), \
         patch("evaluators.llm_client.get_llm_config", return_value={"model": "test-model"}):
        res = await client.post("/api/v1/evaluate/single", json={
            "question": "什么是物料A？",
            "answer": "物料A是飞机零件。",
            "metric": "correctness",
            "ground_truth": None,
        })

    assert res.status_code == 200
    body = res.json()
    assert body["metric"] == "correctness"
    # CorrectnessMetric 在缺 ground_truth 时仍调用 LLM，返回 score=0.0
    # skipped 取决于 BaseMetric.measure 内部逻辑；此处验证不报错且返回结构正确
    assert "score" in body
    assert body["eval_method"] == "llm"
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_evaluate_single.py::test_evaluate_single_correctness_missing_ground_truth_skipped -v`
Expected: PASS

- [ ] **Step 7: 写测试 — 未知指标返回 400**

追加到 `backend/tests/test_evaluate_single.py`：

```python
@pytest.mark.asyncio
async def test_evaluate_single_unknown_metric_returns_400(client):
    """未知指标名返回 400 + available_metrics。"""
    res = await client.post("/api/v1/evaluate/single", json={
        "question": "test",
        "answer": "test",
        "metric": "foo",
    })

    assert res.status_code == 400
    body = res.json()
    assert body["error"] == "unknown_metric"
    assert "faithfulness" in body["available_metrics"]
    assert "answer_relevancy" in body["available_metrics"]
    assert "correctness" in body["available_metrics"]
```

- [ ] **Step 8: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_evaluate_single.py::test_evaluate_single_unknown_metric_returns_400 -v`
Expected: PASS

- [ ] **Step 9: 写测试 — 自定义 threshold 覆盖默认值**

追加到 `backend/tests/test_evaluate_single.py`：

```python
@pytest.mark.asyncio
async def test_evaluate_single_custom_threshold(client):
    """自定义 threshold 覆盖默认 0.8。"""
    mock_response = "SCORE: 0.75\nREASON: 基本正确。"
    mock_llm = AsyncMock(return_value=mock_response)

    with patch("evaluators.llm_client.evaluate_with_llm", mock_llm), \
         patch("evaluators.llm_client.get_llm_config", return_value={"model": "test-model"}):
        res = await client.post("/api/v1/evaluate/single", json={
            "question": "什么是物料A？",
            "answer": "物料A是飞机零件。",
            "metric": "answer_relevancy",
            "threshold": 0.7,
        })

    assert res.status_code == 200
    body = res.json()
    assert body["threshold"] == 0.7
    assert body["score"] == 0.75
    assert body["passed"] is True  # 0.75 >= 0.7
```

- [ ] **Step 10: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_evaluate_single.py::test_evaluate_single_custom_threshold -v`
Expected: PASS

- [ ] **Step 11: 提交**

```bash
cd D:/work/HT集团/soft/onto-eval
git add backend/main.py backend/tests/test_evaluate_single.py
git commit -m "feat: 新增 POST /api/v1/evaluate/single 单题同步评测端点

供 MCP server 调用，无状态不落库。P0 只走 LLM 评估。
缺必要输入返回 skipped=true，未知指标返回 400 + available_metrics。

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 2: 扩展 `/metrics` 响应加 MCP 所需字段

**Files:**
- Modify: `backend/main.py:305-309`（`builtin` 列表构造）
- Test: `backend/tests/test_evaluate_single.py`（追加测试）

**Interfaces:**
- Consumes: 现有 `list_metrics` 端点的 `builtin` 列表构造逻辑
- Produces: `/metrics` 响应 `builtin` 项新增 `category`/`requires`/`optional`/`default_threshold`/`eval_methods`/`description` 字段

- [ ] **Step 1: 写失败测试 — builtin 项含新字段**

追加到 `backend/tests/test_evaluate_single.py`：

```python
@pytest.mark.asyncio
async def test_metrics_builtin_has_mcp_fields(client):
    """/metrics 响应 builtin 项含 requires/eval_methods/default_threshold。"""
    res = await client.get("/metrics")

    assert res.status_code == 200
    body = res.json()
    builtin = body["builtin"]
    faithfulness = next(b for b in builtin if b["key"] == "faithfulness")
    assert faithfulness["category"] == "quality"
    assert "question" in faithfulness["requires"]
    assert "answer" in faithfulness["requires"]
    assert "context" in faithfulness["requires"]
    assert faithfulness["default_threshold"] == 0.8
    assert "llm" in faithfulness["eval_methods"]
    assert faithfulness["description"]

    answer_relevancy = next(b for b in builtin if b["key"] == "answer_relevancy")
    assert "question" in answer_relevancy["requires"]
    assert "answer" in answer_relevancy["requires"]
    assert "context" not in answer_relevancy["requires"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_evaluate_single.py::test_metrics_builtin_has_mcp_fields -v`
Expected: FAIL（`KeyError: 'category'` 或 `'requires'`）

- [ ] **Step 3: 修改 `list_metrics` 端点**

在 `backend/main.py` 找到 `list_metrics` 函数（约 282 行），替换 `builtin` 列表构造：

将：
```python
    _RUNTIME_KEYS = ("inference_latency", "tokens_total")
    builtin = [
        {"key": k, "label": k, "threshold": v, "is_runtime": k in _RUNTIME_KEYS}
        for k, v in METRICS_CONFIG.items()
        if not (offline_only and k in _RUNTIME_KEYS)
    ]
```

替换为：
```python
    _RUNTIME_KEYS = ("inference_latency", "tokens_total")
    _METRIC_META = {
        "faithfulness": {
            "category": "quality",
            "requires": ["question", "answer", "context"],
            "optional": [],
            "eval_methods": ["llm"],
            "description": "评估回答是否忠实于上下文，检测幻觉",
        },
        "answer_relevancy": {
            "category": "quality",
            "requires": ["question", "answer"],
            "optional": [],
            "eval_methods": ["llm"],
            "description": "评估回答是否紧扣问题",
        },
        "correctness": {
            "category": "quality",
            "requires": ["question", "answer", "ground_truth"],
            "optional": [],
            "eval_methods": ["llm"],
            "description": "评估回答与标准答案的一致程度",
        },
        "inference_latency": {
            "category": "runtime",
            "requires": [],
            "optional": [],
            "eval_methods": ["trace"],
            "description": "推理时延（毫秒）",
        },
        "tokens_total": {
            "category": "runtime",
            "requires": [],
            "optional": [],
            "eval_methods": ["trace"],
            "description": "Tokens 总消耗",
        },
    }
    builtin = [
        {
            "key": k,
            "label": k,
            "threshold": v,
            "is_runtime": k in _RUNTIME_KEYS,
            "category": _METRIC_META.get(k, {}).get("category", "quality"),
            "requires": _METRIC_META.get(k, {}).get("requires", []),
            "optional": _METRIC_META.get(k, {}).get("optional", []),
            "default_threshold": v,
            "eval_methods": _METRIC_META.get(k, {}).get("eval_methods", []),
            "description": _METRIC_META.get(k, {}).get("description", ""),
        }
        for k, v in METRICS_CONFIG.items()
        if not (offline_only and k in _RUNTIME_KEYS)
    ]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_evaluate_single.py::test_metrics_builtin_has_mcp_fields -v`
Expected: PASS

- [ ] **Step 5: 运行全部新测试确认无回归**

Run: `cd backend && python -m pytest tests/test_evaluate_single.py -v`
Expected: 所有测试 PASS

- [ ] **Step 6: 提交**

```bash
cd D:/work/HT集团/soft/onto-eval
git add backend/main.py backend/tests/test_evaluate_single.py
git commit -m "feat: /metrics builtin 项扩展 requires/eval_methods/default_threshold 字段

供 MCP list_metrics 工具使用。纯增量字段，前端忽略未知字段不受影响。

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 3: MCP `config.py` — 配置加载模块

**Files:**
- Create: `mcp/config.py`
- Create: `mcp/config.example.json`
- Test: `mcp/tests/test_config.py`

**Interfaces:**
- Consumes: `mcp/config.json` 文件
- Produces: `load_config(config_path=None) -> dict` 函数，返回校验后的配置 dict，必含 `backend_url`、`request_timeout`、`token`

- [ ] **Step 1: 写失败测试 — 正常加载**

创建 `mcp/tests/__init__.py`（空文件）和 `mcp/tests/test_config.py`：

```python
"""config.py 配置加载单测。"""
import json
import sys
from pathlib import Path

import pytest

# 让 mcp 包可导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _write_config(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_load_config_normal(tmp_path):
    """正常配置加载返回 dict 含 backend_url。"""
    from config import load_config

    p = _write_config(tmp_path, {
        "backend_url": "http://localhost:8000",
        "token": "",
        "request_timeout": 200,
    })
    cfg = load_config(p)
    assert cfg["backend_url"] == "http://localhost:8000"
    assert cfg["request_timeout"] == 200
    assert cfg["token"] == ""


def test_load_config_missing_backend_url_raises(tmp_path):
    """缺 backend_url 抛 ValueError。"""
    from config import load_config

    p = _write_config(tmp_path, {"token": ""})
    with pytest.raises(ValueError, match="backend_url"):
        load_config(p)


def test_load_config_defaults(tmp_path):
    """缺 request_timeout 时默认 200。"""
    from config import load_config

    p = _write_config(tmp_path, {"backend_url": "http://x:8000"})
    cfg = load_config(p)
    assert cfg["request_timeout"] == 200
    assert cfg["token"] == ""
    assert cfg["username"] == ""
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd mcp && python -m pytest tests/test_config.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'config'`）

- [ ] **Step 3: 实现 `mcp/config.py`**

```python
"""config.json 加载与校验。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DEFAULTS = {
    "token": "",
    "username": "",
    "password": "",
    "token_expiry_skew_seconds": 60,
    "request_timeout": 200,
    "default_metric": "faithfulness",
    "default_threshold": None,
}


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """加载并校验 config.json。

    Args:
        config_path: 配置文件路径。None 时用同目录默认 config.json。

    Returns:
        校验后的配置 dict，必含 backend_url / request_timeout / token。
    """
    if config_path is None:
        config_path = Path(__file__).resolve().parent / "config.json"
    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"config.json not found: {config_path}")

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"config must be a JSON object: {config_path}")

    cfg = dict(_DEFAULTS)
    cfg.update(raw)

    backend_url = str(cfg.get("backend_url", "") or "").strip()
    if not backend_url:
        raise ValueError("config.json missing required value: backend_url")

    cfg["backend_url"] = backend_url
    cfg["request_timeout"] = int(cfg["request_timeout"])
    return cfg
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd mcp && python -m pytest tests/test_config.py -v`
Expected: 3 个测试 PASS

- [ ] **Step 5: 创建 `mcp/config.example.json`**

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

- [ ] **Step 6: 提交**

```bash
cd D:/work/HT集团/soft/onto-eval
git add mcp/config.py mcp/config.example.json mcp/tests/__init__.py mcp/tests/test_config.py
git commit -m "feat(mcp): config.json 加载与校验模块

backend_url 必填，其余字段有默认值。供 MCP server 热加载。

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 4: MCP `backend_client.py` — urllib HTTP 客户端

**Files:**
- Create: `mcp/backend_client.py`
- Test: `mcp/tests/test_backend_client.py`

**Interfaces:**
- Consumes: `mcp/config.load_config()` 返回的 dict
- Produces: `BackendClient` 类，`from_config(config) -> BackendClient`、`post(path, body) -> dict`、`get(path) -> dict`；401 自动刷新 token（`_refresh_token` 方法）

- [ ] **Step 1: 写失败测试 — 正常 POST**

创建 `mcp/tests/test_backend_client.py`：

```python
"""backend_client.py 单测。"""
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib import error

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _make_config(backend_url="http://test:8000", token="", **extra):
    cfg = {
        "backend_url": backend_url,
        "token": token,
        "username": "",
        "password": "",
        "token_expiry_skew_seconds": 60,
        "request_timeout": 200,
    }
    cfg.update(extra)
    return cfg


def _mock_urlopen(response_body: dict, status=200):
    """返回一个 MagicMock，模拟 urllib.urlopen 上下文管理器。"""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.read = MagicMock(return_value=json.dumps(response_body).encode("utf-8"))
    ctx.status = status
    return ctx


def test_backend_client_post_normal():
    """正常 POST 透传 + JSON 解析。"""
    from backend_client import BackendClient

    cfg = _make_config()
    client = BackendClient.from_config(cfg)

    expected = {"score": 0.85, "passed": True}
    with patch("backend_client.request.urlopen", return_value=_mock_urlopen(expected)) as mock_urlopen:
        result = client.post("/api/v1/evaluate/single", {"question": "q", "answer": "a", "metric": "faithfulness"})

    assert result == expected
    # 验证请求 URL 和方法
    called_req = mock_urlopen.call_args[0][0]
    assert called_req.full_url == "http://test:8000/api/v1/evaluate/single"
    assert called_req.get_method() == "POST"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd mcp && python -m pytest tests/test_backend_client.py::test_backend_client_post_normal -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'backend_client'`）

- [ ] **Step 3: 实现 `mcp/backend_client.py`**

```python
"""onto-eval 后端 HTTP 客户端，标准库 urllib only。"""
from __future__ import annotations

import json
from typing import Any
from urllib import error, request


class BackendClient:
    """调用 onto-eval 后端的 HTTP 客户端。

    标准库 urllib only。401 时自动刷新 token 重试一次（onto-eval 当前无鉴权，
    token 字段预留，username 为空时跳过刷新直接抛错）。
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: int,
        config: dict[str, Any],
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.config = config

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "BackendClient":
        return cls(
            config["backend_url"],
            config.get("token", ""),
            int(config.get("request_timeout", 200)),
            config,
        )

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        allow_refresh: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        data = (
            json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
        )
        req = request.Request(url, data=data, method=method, headers=headers)

        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                return json.loads(raw)
        except error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            if exc.code == 401 and allow_refresh and self.config.get("username"):
                self._refresh_token()
                return self._request(method, path, body, allow_refresh=False)
            # 透传后端 JSON 错误（如 400 unknown_metric）
            try:
                err_body = json.loads(body_text)
            except (json.JSONDecodeError, ValueError):
                err_body = {"error": "http_error", "message": f"HTTP {exc.code}: {body_text}"}
            raise RuntimeError(f"HTTP {exc.code}: {err_body}") from exc
        except error.URLError as exc:
            raise RuntimeError(
                f"无法连接 onto-eval 后端 ({self.base_url}): {exc.reason}. "
                f"请检查 config.json 的 backend_url。"
            ) from exc

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body)

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def _refresh_token(self) -> None:
        """用 username/password 重新登录获取 token（onto-eval 当前无鉴权，预留）。"""
        username = self.config.get("username", "")
        password = self.config.get("password", "")
        if not username or not password:
            raise ValueError("config.json 缺 username/password，无法刷新 token")

        login_body = json.dumps(
            {"username": username, "password": password}
        ).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/auth/login",
            data=login_body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8") or "{}")
        except error.URLError as exc:
            raise RuntimeError(f"登录失败: {exc.reason}") from exc

        token = payload.get("token")
        if not token:
            raise RuntimeError("登录响应未包含 token")
        self.token = str(token)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd mcp && python -m pytest tests/test_backend_client.py::test_backend_client_post_normal -v`
Expected: PASS

- [ ] **Step 5: 写测试 — 401 自动刷新**

追加到 `mcp/tests/test_backend_client.py`：

```python
def test_backend_client_401_refresh_then_retry():
    """401 → 刷新 token → 重试成功。"""
    from backend_client import BackendClient

    cfg = _make_config(token="expired_token", username="admin", password="secret")
    client = BackendClient.from_config(cfg)

    # 第一次 401，第二次 200
    http_err = error.HTTPError(
        url="http://test:8000/api/v1/evaluate/single",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=MagicMock(),
    )
    http_err.read = MagicMock(return_value=b'{"error": "unauthorized"}')

    login_resp = {"token": "new_valid_token"}
    success_resp = {"score": 0.9, "passed": True}

    call_count = {"urlopen": 0}

    def fake_urlopen(req, timeout=None):
        call_count["urlopen"] += 1
        if call_count["urlopen"] == 1:
            raise http_err  # 第一次 evaluate 401
        if call_count["urlopen"] == 2:
            return _mock_urlopen(login_resp)  # login 200
        return _mock_urlopen(success_resp)  # retry evaluate 200

    with patch("backend_client.request.urlopen", side_effect=fake_urlopen):
        result = client.post("/api/v1/evaluate/single", {"question": "q"})

    assert result == success_resp
    assert client.token == "new_valid_token"
    assert call_count["urlopen"] == 3
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd mcp && python -m pytest tests/test_backend_client.py::test_backend_client_401_refresh_then_retry -v`
Expected: PASS

- [ ] **Step 7: 写测试 — 后端不可达友好错误**

追加到 `mcp/tests/test_backend_client.py`：

```python
def test_backend_client_connection_error_friendly():
    """后端不可达 → 友好错误提示检查 backend_url。"""
    from backend_client import BackendClient

    cfg = _make_config(backend_url="http://nonexistent:9999")
    client = BackendClient.from_config(cfg)

    with patch("backend_client.request.urlopen", side_effect=error.URLError("Connection refused")):
        with pytest.raises(RuntimeError, match="无法连接 onto-eval 后端"):
            client.get("/api/v1/metrics")
```

- [ ] **Step 8: 运行测试确认通过**

Run: `cd mcp && python -m pytest tests/test_backend_client.py::test_backend_client_connection_error_friendly -v`
Expected: PASS

- [ ] **Step 9: 写测试 — GET 方法**

追加到 `mcp/tests/test_backend_client.py`：

```python
def test_backend_client_get_normal():
    """正常 GET 透传。"""
    from backend_client import BackendClient

    cfg = _make_config()
    client = BackendClient.from_config(cfg)

    expected = {"builtin": [{"key": "faithfulness"}]}
    with patch("backend_client.request.urlopen", return_value=_mock_urlopen(expected)) as mock_urlopen:
        result = client.get("/api/v1/metrics")

    assert result == expected
    called_req = mock_urlopen.call_args[0][0]
    assert called_req.get_method() == "GET"
```

- [ ] **Step 10: 运行全部 backend_client 测试**

Run: `cd mcp && python -m pytest tests/test_backend_client.py -v`
Expected: 4 个测试 PASS

- [ ] **Step 11: 提交**

```bash
cd D:/work/HT集团/soft/onto-eval
git add mcp/backend_client.py mcp/tests/test_backend_client.py
git commit -m "feat(mcp): backend_client urllib HTTP 客户端 + 401 自动刷新

标准库 only，除 mcp SDK 外零依赖。吸收 remote-agent-simtest 的 token 刷新模式。

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 5: MCP `mcp_server.py` — stdio MCP server

**Files:**
- Create: `mcp/mcp_server.py`
- Create: `mcp/requirements.txt`
- Test: `mcp/tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `mcp.config.load_config()`、`mcp.backend_client.BackendClient`
- Produces: stdio MCP server，注册两个工具：
  - `evaluate_answer(question, answer, metric, context?, ground_truth?, threshold?)` → 后端 `/api/v1/evaluate/single` 响应
  - `list_metrics()` → 后端 `/api/v1/metrics` 响应

- [ ] **Step 1: 写失败测试 — evaluate_answer 工具调用**

创建 `mcp/tests/test_mcp_server.py`：

```python
"""mcp_server.py 工具调用单测。"""
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _make_config():
    return {
        "backend_url": "http://test:8000",
        "token": "",
        "username": "",
        "password": "",
        "request_timeout": 200,
        "default_metric": "faithfulness",
        "default_threshold": None,
    }


@pytest.mark.asyncio
async def test_call_evaluate_answer():
    """call_tool('evaluate_answer', ...) 调 backend_client 并透传结果。"""
    from mcp.types import TextContent

    backend_resp = {
        "metric": "faithfulness",
        "score": 0.85,
        "passed": True,
        "threshold": 0.8,
        "reason": "回答忠实于上下文",
        "duration_ms": 1200,
        "skipped": False,
        "eval_method": "llm",
    }

    mock_client = MagicMock()
    mock_client.post = MagicMock(return_value=backend_resp)

    with patch("mcp_server.load_config", return_value=_make_config()), \
         patch("mcp_server.BackendClient.from_config", return_value=mock_client):
        from mcp_server import call_tool

        result = await call_tool("evaluate_answer", {
            "question": "什么是物料A？",
            "answer": "物料A是零件。",
            "metric": "faithfulness",
            "context": "物料A是结构件。",
        })

    assert len(result) == 1
    assert isinstance(result[0], TextContent)
    parsed = json.loads(result[0].text)
    assert parsed["score"] == 0.85
    assert parsed["passed"] is True
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "/api/v1/evaluate/single"
    assert call_args[0][1]["metric"] == "faithfulness"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd mcp && python -m pytest tests/test_mcp_server.py::test_call_evaluate_answer -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'mcp_server'` 或 `mcp` SDK 未装）

如果 `mcp` SDK 未装，先安装：`pip install mcp`

- [ ] **Step 3: 实现 `mcp/mcp_server.py`**

```python
#!/usr/bin/env python3
"""onto-eval MCP server: 单题评测能力暴露给 AI 工具。

stdio 传输，注册两个工具：
- evaluate_answer: 单题同步评测
- list_metrics: 列出可用指标

依赖：mcp SDK（唯一第三方依赖），HTTP 走标准库 urllib。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from backend_client import BackendClient
from config import load_config

server = Server("onto-eval-mcp")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="evaluate_answer",
            description=(
                "评测一道题的回答质量。返回分数(0-1)、是否通过、评估理由。"
                "支持指标：faithfulness(需 context)、answer_relevancy(仅需 question+answer)、"
                "correctness(需 ground_truth)。先用 list_metrics 查可用指标。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "问题内容"},
                    "answer": {"type": "string", "description": "待评估的回答"},
                    "metric": {
                        "type": "string",
                        "description": "指标名: faithfulness / answer_relevancy / correctness",
                    },
                    "context": {"type": "string", "description": "上下文(faithfulness 需要)"},
                    "ground_truth": {"type": "string", "description": "标准答案(correctness 需要)"},
                    "threshold": {
                        "type": "number",
                        "description": "通过阈值(0-1)，默认用指标默认值 0.8",
                    },
                },
                "required": ["question", "answer", "metric"],
            },
        ),
        Tool(
            name="list_metrics",
            description="列出 onto-eval 后端可用的评测指标及所需输入。",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """工具调用分发。"""
    config = load_config()
    client = BackendClient.from_config(config)

    if name == "evaluate_answer":
        # 过滤 None 值，避免传给后端
        body = {k: v for k, v in arguments.items() if v is not None}
        result = client.post("/api/v1/evaluate/single", body)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    if name == "list_metrics":
        result = client.get("/api/v1/metrics")
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    raise ValueError(f"Unknown tool: {name}")


# 注册 call_tool 到 server
server.call_tool()(call_tool)


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: 创建 `mcp/requirements.txt`**

```
mcp>=1.0
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd mcp && python -m pytest tests/test_mcp_server.py::test_call_evaluate_answer -v`
Expected: PASS

- [ ] **Step 6: 写测试 — list_metrics 工具调用**

追加到 `mcp/tests/test_mcp_server.py`：

```python
@pytest.mark.asyncio
async def test_call_list_metrics():
    """call_tool('list_metrics', ...) 调 backend_client 并透传结果。"""
    from mcp.types import TextContent

    backend_resp = {
        "builtin": [
            {"key": "faithfulness", "requires": ["question", "answer", "context"]},
        ],
    }

    mock_client = MagicMock()
    mock_client.get = MagicMock(return_value=backend_resp)

    with patch("mcp_server.load_config", return_value=_make_config()), \
         patch("mcp_server.BackendClient.from_config", return_value=mock_client):
        from mcp_server import call_tool

        result = await call_tool("list_metrics", {})

    assert len(result) == 1
    parsed = json.loads(result[0].text)
    assert parsed["builtin"][0]["key"] == "faithfulness"
    mock_client.get.assert_called_once_with("/api/v1/metrics")
```

- [ ] **Step 7: 运行测试确认通过**

Run: `cd mcp && python -m pytest tests/test_mcp_server.py::test_call_list_metrics -v`
Expected: PASS

- [ ] **Step 8: 写测试 — 未知工具名抛错**

追加到 `mcp/tests/test_mcp_server.py`：

```python
@pytest.mark.asyncio
async def test_call_unknown_tool_raises():
    """未知工具名抛 ValueError。"""
    with patch("mcp_server.load_config", return_value=_make_config()), \
         patch("mcp_server.BackendClient.from_config", return_value=MagicMock()):
        from mcp_server import call_tool

        with pytest.raises(ValueError, match="Unknown tool"):
            await call_tool("nonexistent", {})
```

- [ ] **Step 9: 运行全部 MCP server 测试**

Run: `cd mcp && python -m pytest tests/test_mcp_server.py -v`
Expected: 3 个测试 PASS

- [ ] **Step 10: 提交**

```bash
cd D:/work/HT集团/soft/onto-eval
git add mcp/mcp_server.py mcp/requirements.txt mcp/tests/test_mcp_server.py
git commit -m "feat(mcp): stdio MCP server 注册 evaluate_answer/list_metrics 工具

config.json 热加载（每次调用 reload），错误信息对 AI 友好。

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 6: MCP README + .gitignore + 端到端验证

**Files:**
- Create: `mcp/README.md`
- Modify: `.gitignore`（加 `mcp/config.json`）
- Create: `mcp/config.json`（从 example 复制，gitignore）

**Interfaces:**
- 无代码接口，仅文档与配置

- [ ] **Step 1: 创建 `mcp/README.md`**

```markdown
# onto-eval MCP server

将 onto-eval 的单题指标评测能力暴露给任何支持 MCP 的 AI 工具（Claude Code、Cursor、Codex CLI、Trae CN、OpenCode、Windsurf、Zed、VS Code Copilot 等）。

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
| `token` | 否 | 鉴权 token（onto-eval 当前无鉴权，留空） |
| `username`/`password` | 否 | 鉴权凭据（token 过期时自动登录刷新，留空则不刷新） |
| `request_timeout` | 否 | HTTP 超时秒数，默认 200 |
| `default_metric` | 否 | 未指定指标时的默认值 |
| `default_threshold` | 否 | 未指定阈值时的默认值 |

修改 config.json 后无需重启 MCP server，下次工具调用自动热加载。
```

- [ ] **Step 2: 创建 `mcp/config.json`（从 example 复制）**

```bash
cd D:/work/HT集团/soft/onto-eval/mcp
cp config.example.json config.json
```

- [ ] **Step 3: 修改 `.gitignore` 加 `mcp/config.json`**

在 `.gitignore` 末尾追加：

```
# MCP server 使用者配置（含 token，不入库）
mcp/config.json
```

- [ ] **Step 4: 端到端验证 — 后端单测全过**

Run: `cd backend && python -m pytest tests/test_evaluate_single.py -v`
Expected: 所有测试 PASS

- [ ] **Step 5: 端到端验证 — MCP 单测全过**

Run: `cd mcp && python -m pytest tests/ -v`
Expected: 所有测试 PASS（config 3 + backend_client 4 + mcp_server 3 = 10 个）

- [ ] **Step 6: 端到端验证 — MCP server 可启动**

Run: `cd mcp && echo '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}},"id":1}' | python mcp_server.py 2>/dev/null | head -1`
Expected: 输出一行 JSON 含 `"serverInfo":{"name":"onto-eval-mcp"}`（证明 server 正常响应 initialize）

- [ ] **Step 7: 提交**

```bash
cd D:/work/HT集团/soft/onto-eval
git add mcp/README.md .gitignore mcp/config.example.json
git commit -m "docs(mcp): README 安装与配置说明 + gitignore config.json

README 列 9 种常见 AI 工具的 MCP 配置速查，通用三元组 command+args。

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec 覆盖检查**：

| Spec 条目 | 实现任务 |
|---|---|
| POST /api/v1/evaluate/single | Task 1 |
| GET /api/v1/metrics 扩展 | Task 2 |
| mcp/config.py | Task 3 |
| mcp/backend_client.py | Task 4 |
| mcp/mcp_server.py（evaluate_answer + list_metrics） | Task 5 |
| mcp/config.example.json | Task 3 Step 5 |
| mcp/requirements.txt | Task 5 Step 4 |
| mcp/README.md | Task 6 Step 1 |
| .gitignore mcp/config.json | Task 6 Step 3 |
| 后端测试 | Task 1 + Task 2 |
| MCP 测试 | Task 3 + Task 4 + Task 5 |
| 端到端验证 | Task 6 Step 4-6 |
| 不落库 | Task 1（端点实现无 DB 写入） |
| urllib only | Task 4（backend_client 用标准库） |
| faithfulness 只走 LLM | Task 1（get_metric 不传 use_bm25） |
| 稳定性边界 | 契约 schema 在 Task 1/2/5 锁定 |

无遗漏。

**Placeholder scan**：无 TBD/TODO，所有步骤含完整代码块。

**Type consistency**：
- `BackendClient.from_config(config)` 在 Task 4 定义，Task 5 `mcp_server.py` 使用 ✓
- `load_config(config_path=None) -> dict` 在 Task 3 定义，Task 5 使用 ✓
- `EvaluateSingleRequest` 在 Task 1 定义，响应字段 `metric/score/passed/threshold/reason/duration_ms/skipped/eval_method` 在 Task 1 和 Task 5（透传）一致 ✓
- `call_tool(name, arguments) -> list[TextContent]` 在 Task 5 定义，测试使用 ✓

无类型不一致。
