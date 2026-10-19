"""inference_metrics / runtime_metrics 单元测试。"""
from evaluators.runtime_metrics import (
    build_runtime_metric_values,
    is_runtime_metric,
    score_runtime_metric,
    split_metrics,
)
from services.inference_metrics import (
    extract_inference_runtime_fields,
    extract_inference_token_breakdown,
)


def test_extract_inference_runtime_fields_from_sync_body():
    body = {
        "final_answer": "ok",
        "duration_ms": 4200,
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }
    duration, tokens = extract_inference_runtime_fields(body)
    assert duration == 4200
    assert tokens == 150


def test_extract_inference_runtime_fields_sum_usage():
    body = {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    duration, tokens = extract_inference_runtime_fields(body)
    assert duration is None
    assert tokens == 15


def test_extract_inference_runtime_fields_from_nested_output_usage():
    body = {
        "output": {
            "final_answer": "ok",
            "elapsed_ms": 1234,
            "usage": {"promptTokens": 20, "completionTokens": 8},
        }
    }
    duration, tokens = extract_inference_runtime_fields(body)
    assert duration == 1234
    assert tokens == 28


def test_extract_inference_runtime_fields_from_metrics_camel_case():
    body = {
        "metrics": {
            "durationMs": 2000,
            "usage": {"totalTokens": 42},
        }
    }
    duration, tokens = extract_inference_runtime_fields(body)
    assert duration == 2000
    assert tokens == 42


def test_extract_inference_token_breakdown_from_explicit_fields():
    body = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "tokens_llm": 140,
            "tokens_tool": 10,
        }
    }
    llm, tool, usage = extract_inference_token_breakdown(body)
    assert llm == 140
    assert tool == 10
    assert usage["total_tokens"] == 150


def test_extract_inference_token_breakdown_derives_llm_from_prompt_completion():
    body = {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "tokens_tool": 0}}
    llm, tool, usage = extract_inference_token_breakdown(body)
    assert llm == 15
    assert tool is None
    assert usage is not None


def test_normalize_reported_submetric_treats_zero_as_unreported():
    from services.inference_metrics import normalize_reported_submetric

    assert normalize_reported_submetric(0) is None
    assert normalize_reported_submetric(None) is None
    assert normalize_reported_submetric(12) == 12


def test_runtime_metric_scoring_latency():
    raw, detail = build_runtime_metric_values(
        "inference_latency", inference_duration_ms=3000, tokens_total=None
    )
    assert raw == 3000.0
    assert "3000" in detail
    normalized, passed, _ = score_runtime_metric("inference_latency", raw, 5000)
    assert passed is True
    assert normalized == 1.0


def test_runtime_metric_scoring_degrades_between_target_and_limit():
    normalized, passed, _ = score_runtime_metric("inference_latency", 10000, 5000)
    assert passed is False
    assert normalized == 0.5


def test_runtime_metric_scoring_zero_at_limit():
    normalized, passed, _ = score_runtime_metric("tokens_total", 3000, 1000)
    assert passed is False
    assert normalized == 0.0


def test_runtime_metric_unknown_tokens_fails():
    normalized, passed, _ = score_runtime_metric("tokens_total", None, 1000)
    assert passed is False
    assert normalized == 0.0


def test_split_metrics():
    runtime, qualitative = split_metrics(
        ["correctness", "inference_latency", "faithfulness", "tokens_total"]
    )
    assert runtime == ["inference_latency", "tokens_total"]
    assert qualitative == ["correctness", "faithfulness"]
    assert is_runtime_metric("inference_latency")
