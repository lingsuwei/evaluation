"""POST /api/v1/evaluate/single 端点单测。"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, patch, MagicMock

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
    mock_response = (0.85, "回答完全由参考内容支持。")
    mock_llm = AsyncMock(return_value=mock_response)

    with patch("evaluators.faithfulness.evaluate_metric_with_llm", mock_llm), \
         patch("config.get_llm_config", return_value={"model": "test-model"}), \
         patch("config.get_model_pool", return_value={"test-model": {"model": "test-model"}}), \
         patch("config._db_llm_models_cache", {"test-model": {"model": "test-model", "is_default": True}}), \
         patch("config.get_default_model_key", return_value="test-model"), \
         patch("evaluators.faithfulness._is_model_available", return_value=True):
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


@pytest.mark.asyncio
async def test_evaluate_single_correctness_without_ground_truth(client):
    """correctness 缺 ground_truth 时仍调 LLM 评估，返回 float score，skipped=false。"""
    mock_response = (0.75, "基于通用知识评估。")
    mock_llm = AsyncMock(return_value=mock_response)

    with patch("evaluators.faithfulness.evaluate_metric_with_llm", mock_llm), \
         patch("config.get_llm_config", return_value={"model": "test-model"}), \
         patch("config.get_model_pool", return_value={"test-model": {"model": "test-model"}}), \
         patch("config._db_llm_models_cache", {"test-model": {"model": "test-model", "is_default": True}}), \
         patch("config.get_default_model_key", return_value="test-model"), \
         patch("evaluators.faithfulness._is_model_available", return_value=True):
        res = await client.post("/api/v1/evaluate/single", json={
            "question": "什么是物料A？",
            "answer": "物料A是飞机零件。",
            "metric": "correctness",
            "ground_truth": None,
        })

    assert res.status_code == 200
    body = res.json()
    assert body["metric"] == "correctness"
    assert isinstance(body["score"], float)
    assert body["skipped"] is False
    assert body["passed"] == (body["score"] >= body["threshold"])
    assert body["eval_method"] == "llm"


@pytest.mark.asyncio
async def test_evaluate_single_skipped_when_score_is_none(client):
    """当 metric.measure 返回 score=None 时，端点正确设置 skipped=true。"""
    from evaluators.base_metric import EvaluationResult, BaseMetric

    mock_metric = AsyncMock(spec=BaseMetric)
    mock_result = EvaluationResult(score=None, reason="缺输入", duration_ms=0)
    mock_metric.measure = AsyncMock(return_value=(mock_result, False))

    with patch("evaluators.registry.get_metric", return_value=mock_metric):
        res = await client.post("/api/v1/evaluate/single", json={
            "question": "test",
            "answer": "test",
            "metric": "faithfulness",
        })

    assert res.status_code == 200
    body = res.json()
    assert body["skipped"] is True
    assert body["score"] is None
    assert body["passed"] is False


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


@pytest.mark.asyncio
async def test_evaluate_single_custom_threshold(client):
    """自定义 threshold 覆盖默认 0.8。"""
    mock_response = (0.75, "基本正确。")
    mock_llm = AsyncMock(return_value=mock_response)

    with patch("evaluators.faithfulness.evaluate_metric_with_llm", mock_llm), \
         patch("config.get_llm_config", return_value={"model": "test-model"}), \
         patch("config.get_model_pool", return_value={"test-model": {"model": "test-model"}}), \
         patch("config._db_llm_models_cache", {"test-model": {"model": "test-model", "is_default": True}}), \
         patch("config.get_default_model_key", return_value="test-model"), \
         patch("evaluators.faithfulness._is_model_available", return_value=True):
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


@pytest.mark.asyncio
async def test_evaluate_single_token_required_when_env_set(client, monkeypatch):
    """EVAL_SINGLE_TOKEN 已设：缺 header → 401；正确 header → 200。"""
    monkeypatch.setenv("EVAL_SINGLE_TOKEN", "secret-abc")

    # 1. 缺 header → 401
    res_no_header = await client.post("/api/v1/evaluate/single", json={
        "question": "q", "answer": "a", "metric": "faithfulness",
    })
    assert res_no_header.status_code == 401
    body = res_no_header.json()
    assert body["error"] == "invalid_token"

    # 2. 错误 header → 401
    res_wrong = await client.post(
        "/api/v1/evaluate/single",
        json={"question": "q", "answer": "a", "metric": "faithfulness"},
        headers={"X-Eval-Token": "wrong"},
    )
    assert res_wrong.status_code == 401

    # 3. 正确 header → 200（mock LLM）
    mock_response = (0.9, "ok")
    mock_llm = AsyncMock(return_value=mock_response)
    with patch("evaluators.faithfulness.evaluate_metric_with_llm", mock_llm), \
         patch("config.get_llm_config", return_value={"model": "test-model"}), \
         patch("config.get_model_pool", return_value={"test-model": {"model": "test-model"}}), \
         patch("config._db_llm_models_cache", {"test-model": {"model": "test-model", "is_default": True}}), \
         patch("config.get_default_model_key", return_value="test-model"), \
         patch("evaluators.faithfulness._is_model_available", return_value=True):
        res_ok = await client.post(
            "/api/v1/evaluate/single",
            json={
                "question": "q", "answer": "a", "metric": "faithfulness",
                "context": "ctx",
            },
            headers={"X-Eval-Token": "secret-abc"},
        )
    assert res_ok.status_code == 200
    assert res_ok.json()["score"] == 0.9


@pytest.mark.asyncio
async def test_evaluate_single_open_when_env_unset(client, monkeypatch):
    """EVAL_SINGLE_TOKEN 未设：无 header 也能访问（本地开发无负担）。"""
    monkeypatch.delenv("EVAL_SINGLE_TOKEN", raising=False)

    mock_response = (0.8, "ok")
    mock_llm = AsyncMock(return_value=mock_response)
    with patch("evaluators.faithfulness.evaluate_metric_with_llm", mock_llm), \
         patch("config.get_llm_config", return_value={"model": "test-model"}), \
         patch("config.get_model_pool", return_value={"test-model": {"model": "test-model"}}), \
         patch("config._db_llm_models_cache", {"test-model": {"model": "test-model", "is_default": True}}), \
         patch("config.get_default_model_key", return_value="test-model"), \
         patch("evaluators.faithfulness._is_model_available", return_value=True):
        res = await client.post("/api/v1/evaluate/single", json={
            "question": "q", "answer": "a", "metric": "faithfulness", "context": "ctx",
        })
    assert res.status_code == 200
    assert res.json()["score"] == 0.8