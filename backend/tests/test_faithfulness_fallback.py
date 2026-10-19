"""忠诚度指标（faithfulness）兜底逻辑单元测试。

覆盖场景：
1. BM25 Context 模式：无参考内容时返回中性分 0.5
2. BM25 文档库模式：文档库为空时返回中性分 0.5
3. BM25 模式：有参考内容时正常评估（非降级）
4. LLM 模式：无参考内容时切换为自包含事实一致性评估提示词
5. LLM 模式：有参考内容时使用正常提示词
6. 管线层 _judge_quality_metric_once：无参考时记录 WARN 日志
"""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, ".")

from evaluators.base_metric import EvaluationResult
from evaluators.faithfulness import FaithfulnessMetric


# ── BM25 Context 模式 ──────────────────────────────────────────────


async def test_bm25_context_no_reference_returns_neutral_score():
    """无 context 且无 ground_truth 时，BM25 Context 模式返回中性分 0.5"""
    metric = FaithfulnessMetric(use_bm25=True, use_doc_library=False)
    result = await metric.evaluate_with_bm25(
        question="什么是物料A的用途？",
        answer="物料A用于制造飞机机翼。",
        context=None,
        ground_truth=None,
    )
    assert result.score == 0.5
    assert "降级" in result.reason


async def test_bm25_context_empty_strings_returns_neutral_score():
    """context 和 ground_truth 均为空字符串时，返回中性分 0.5"""
    metric = FaithfulnessMetric(use_bm25=True, use_doc_library=False)
    result = await metric.evaluate_with_bm25(
        question="什么是物料A的用途？",
        answer="物料A用于制造飞机机翼。",
        context="",
        ground_truth="",
    )
    assert result.score == 0.5
    assert "降级" in result.reason


async def test_bm25_context_with_context_returns_real_score():
    """有 context 时，BM25 Context 模式正常评估"""
    metric = FaithfulnessMetric(use_bm25=True, use_doc_library=False)
    result = await metric.evaluate_with_bm25(
        question="什么是物料A的用途？",
        answer="物料A用于制造飞机机翼。",
        context="物料A用于制造飞机机翼，是重要的结构件。",
        ground_truth=None,
    )
    assert result.score is not None
    assert 0.0 <= result.score <= 1.0
    assert "降级" not in result.reason


async def test_bm25_context_with_ground_truth_returns_real_score():
    """有 ground_truth 但无 context 时，BM25 Context 模式正常评估"""
    metric = FaithfulnessMetric(use_bm25=True, use_doc_library=False)
    result = await metric.evaluate_with_bm25(
        question="什么是物料A的用途？",
        answer="物料A用于制造飞机机翼。",
        context=None,
        ground_truth="物料A用于制造飞机机翼，是重要的结构件。",
    )
    assert result.score is not None
    assert 0.0 <= result.score <= 1.0
    assert "降级" not in result.reason


# ── BM25 文档库模式 ──────────────────────────────────────────────────


async def test_bm25_library_empty_docs_returns_neutral_score():
    """文档库为空时，BM25 文档库模式返回中性分 0.5"""
    metric = FaithfulnessMetric(
        use_bm25=True, use_doc_library=True, ontology_package=None
    )

    mock_loader = MagicMock()
    mock_loader.get_all_documents.return_value = []

    with patch("evaluators.faithfulness.get_document_loader", return_value=mock_loader):
        result = await metric.evaluate_with_bm25(
            question="什么是物料A？",
            answer="物料A是飞机零件。",
            context=None,
            ground_truth=None,
        )

    assert result.score == 0.5
    assert "降级" in result.reason


async def test_bm25_library_with_docs_returns_real_score():
    """文档库非空时，BM25 文档库模式正常评估"""
    metric = FaithfulnessMetric(
        use_bm25=True, use_doc_library=True, ontology_package="test_pkg"
    )

    mock_loader = MagicMock()
    mock_loader.get_all_documents.return_value = [
        "物料A是飞机机翼的重要结构件。它由高强度合金制成。"
    ]

    with patch("evaluators.faithfulness.get_document_loader", return_value=mock_loader):
        result = await metric.evaluate_with_bm25(
            question="什么是物料A？",
            answer="物料A是飞机机翼的重要结构件。",
            context=None,
            ground_truth=None,
        )

    assert result.score is not None
    assert 0.0 <= result.score <= 1.0
    assert "降级" not in result.reason


# ── LLM 模式 ────────────────────────────────────────────────────────


def _make_llm_mock(response_text: str):
    """创建 evaluate_with_llm 的 mock，返回固定响应"""
    mock = AsyncMock(return_value=response_text)
    return mock


async def test_llm_mode_no_reference_uses_fallback_prompt():
    """无 context 且无 ground_truth 时，LLM 模式切换为自包含事实一致性评估提示词"""
    mock_response = "SCORE: 0.75\nREASON: 回答中的事实声明大部分正确，存在少量存疑内容。"
    mock_llm = _make_llm_mock(mock_response)

    with patch("evaluators.llm_client.evaluate_with_llm", mock_llm), \
         patch("evaluators.llm_client.get_llm_config", return_value={"model": "test-model"}):
        from evaluators.llm_client import evaluate_metric_with_llm

        score, reason = await evaluate_metric_with_llm(
            "faithfulness",
            "什么是物料A的用途？",
            "物料A用于制造飞机机翼。",
            context=None,
            ground_truth=None,
            model_key="test",
        )

    mock_llm.assert_called_once()
    prompt = mock_llm.call_args[0][0]
    assert "自包含事实一致性校验" in prompt
    assert "降级评估" in prompt
    assert score == 0.75


async def test_llm_mode_with_context_uses_normal_prompt():
    """有 context 时，LLM 模式使用正常提示词"""
    mock_response = "SCORE: 0.90\nREASON: 回答完全忠实于参考内容。"
    mock_llm = _make_llm_mock(mock_response)

    with patch("evaluators.llm_client.evaluate_with_llm", mock_llm), \
         patch("evaluators.llm_client.get_llm_config", return_value={"model": "test-model"}):
        from evaluators.llm_client import evaluate_metric_with_llm

        score, reason = await evaluate_metric_with_llm(
            "faithfulness",
            "什么是物料A的用途？",
            "物料A用于制造飞机机翼。",
            context="物料A用于制造飞机机翼，是重要的结构件。",
            ground_truth=None,
            model_key="test",
        )

    mock_llm.assert_called_once()
    prompt = mock_llm.call_args[0][0]
    assert "自包含事实一致性校验" not in prompt
    assert "忠实度" in prompt
    assert score == 0.90


async def test_llm_mode_with_ground_truth_uses_normal_prompt():
    """有 ground_truth 但无 context 时，LLM 模式使用正常提示词"""
    mock_response = "SCORE: 0.85\nREASON: 回答基本忠实于标准答案。"
    mock_llm = _make_llm_mock(mock_response)

    with patch("evaluators.llm_client.evaluate_with_llm", mock_llm), \
         patch("evaluators.llm_client.get_llm_config", return_value={"model": "test-model"}):
        from evaluators.llm_client import evaluate_metric_with_llm

        score, reason = await evaluate_metric_with_llm(
            "faithfulness",
            "什么是物料A的用途？",
            "物料A用于制造飞机机翼。",
            context=None,
            ground_truth="物料A用于制造飞机机翼，是重要的结构件。",
            model_key="test",
        )

    mock_llm.assert_called_once()
    prompt = mock_llm.call_args[0][0]
    assert "自包含事实一致性校验" not in prompt
    assert "忠实度" in prompt
    assert score == 0.85


async def test_llm_mode_empty_strings_uses_fallback_prompt():
    """context 和 ground_truth 均为空字符串时，使用降级提示词"""
    mock_response = "SCORE: 0.60\nREASON: 部分事实声明无法验证。"
    mock_llm = _make_llm_mock(mock_response)

    with patch("evaluators.llm_client.evaluate_with_llm", mock_llm), \
         patch("evaluators.llm_client.get_llm_config", return_value={"model": "test-model"}):
        from evaluators.llm_client import evaluate_metric_with_llm

        score, reason = await evaluate_metric_with_llm(
            "faithfulness",
            "什么是物料A的用途？",
            "物料A用于制造飞机机翼。",
            context="",
            ground_truth="",
            model_key="test",
        )

    mock_llm.assert_called_once()
    prompt = mock_llm.call_args[0][0]
    assert "自包含事实一致性校验" in prompt
    assert score == 0.60


# ── 管线层降级检测日志 ─────────────────────────────────────────────


async def test_pipeline_fallback_warning_bm25_library():
    """管线层 _judge_quality_metric_once 在 BM25+文档库模式无参考时记录 WARN"""
    with patch("services.evaluation_pipeline.log") as mock_log:
        from services.evaluation_pipeline import _judge_quality_metric_once

        async def dummy_evaluate_single(*args, **kwargs):
            return {"score": 0.5, "reason": "降级", "passed": False, "duration_ms": 0}

        await _judge_quality_metric_once(
            task_id="test-task",
            question_id=0,
            model_name="test-model",
            metric_name="faithfulness",
            threshold=0.8,
            question="测试问题",
            model_answer="测试回答",
            context=None,
            ground_truth=None,
            llm_model="test",
            use_bm25=True,
            use_doc_library=True,
            use_script_eval=False,
            custom_script_metric=None,
            evaluate_single=dummy_evaluate_single,
            evaluate_with_script=None,
            ontology_package=None,
        )

    # 验证 WARN 日志被调用
    warn_calls = [c for c in mock_log.call_args_list if c.args and c.args[0] == "WARN"]
    assert len(warn_calls) >= 1
    assert "降级" in warn_calls[0].args[2]


async def test_pipeline_fallback_warning_bm25_no_reference():
    """管线层 _judge_quality_metric_once 在 BM25 无参考时记录 WARN"""
    with patch("services.evaluation_pipeline.log") as mock_log:
        from services.evaluation_pipeline import _judge_quality_metric_once

        async def dummy_evaluate_single(*args, **kwargs):
            return {"score": 0.5, "reason": "降级", "passed": False, "duration_ms": 0}

        await _judge_quality_metric_once(
            task_id="test-task",
            question_id=0,
            model_name="test-model",
            metric_name="faithfulness",
            threshold=0.8,
            question="测试问题",
            model_answer="测试回答",
            context=None,
            ground_truth=None,
            llm_model="test",
            use_bm25=True,
            use_doc_library=False,
            use_script_eval=False,
            custom_script_metric=None,
            evaluate_single=dummy_evaluate_single,
            evaluate_with_script=None,
            ontology_package=None,
        )

    warn_calls = [c for c in mock_log.call_args_list if c.args and c.args[0] == "WARN"]
    assert len(warn_calls) >= 1
    assert "降级" in warn_calls[0].args[2]


async def test_pipeline_fallback_warning_llm_mode():
    """管线层 _judge_quality_metric_once 在 LLM 模式无参考时记录 WARN"""
    with patch("services.evaluation_pipeline.log") as mock_log:
        from services.evaluation_pipeline import _judge_quality_metric_once

        async def dummy_evaluate_single(*args, **kwargs):
            return {"score": 0.7, "reason": "降级", "passed": False, "duration_ms": 0}

        await _judge_quality_metric_once(
            task_id="test-task",
            question_id=0,
            model_name="test-model",
            metric_name="faithfulness",
            threshold=0.8,
            question="测试问题",
            model_answer="测试回答",
            context=None,
            ground_truth=None,
            llm_model="test",
            use_bm25=False,
            use_doc_library=False,
            use_script_eval=False,
            custom_script_metric=None,
            evaluate_single=dummy_evaluate_single,
            evaluate_with_script=None,
            ontology_package=None,
        )

    warn_calls = [c for c in mock_log.call_args_list if c.args and c.args[0] == "WARN"]
    assert len(warn_calls) >= 1
    assert "降级" in warn_calls[0].args[2]
    assert "自包含" in warn_calls[0].args[2]


async def test_pipeline_no_warning_with_reference():
    """有参考内容时，管线层不记录降级 WARN"""
    with patch("services.evaluation_pipeline.log") as mock_log:
        from services.evaluation_pipeline import _judge_quality_metric_once

        async def dummy_evaluate_single(*args, **kwargs):
            return {"score": 0.9, "reason": "正常", "passed": True, "duration_ms": 0}

        await _judge_quality_metric_once(
            task_id="test-task",
            question_id=0,
            model_name="test-model",
            metric_name="faithfulness",
            threshold=0.8,
            question="测试问题",
            model_answer="测试回答",
            context="参考内容",
            ground_truth=None,
            llm_model="test",
            use_bm25=False,
            use_doc_library=False,
            use_script_eval=False,
            custom_script_metric=None,
            evaluate_single=dummy_evaluate_single,
            evaluate_with_script=None,
            ontology_package=None,
        )

    warn_calls = [c for c in mock_log.call_args_list if c.args and c.args[0] == "WARN"]
    assert len(warn_calls) == 0


async def test_pipeline_no_warning_for_non_faithfulness():
    """非 faithfulness 指标不触发降级 WARN（即使无参考内容）"""
    with patch("services.evaluation_pipeline.log") as mock_log:
        from services.evaluation_pipeline import _judge_quality_metric_once

        async def dummy_evaluate_single(*args, **kwargs):
            return {"score": 0.9, "reason": "正常", "passed": True, "duration_ms": 0}

        await _judge_quality_metric_once(
            task_id="test-task",
            question_id=0,
            model_name="test-model",
            metric_name="answer_relevancy",
            threshold=0.8,
            question="测试问题",
            model_answer="测试回答",
            context=None,
            ground_truth=None,
            llm_model="test",
            use_bm25=False,
            use_doc_library=False,
            use_script_eval=False,
            custom_script_metric=None,
            evaluate_single=dummy_evaluate_single,
            evaluate_with_script=None,
            ontology_package=None,
        )

    warn_calls = [c for c in mock_log.call_args_list if c.args and c.args[0] == "WARN"]
    assert len(warn_calls) == 0
