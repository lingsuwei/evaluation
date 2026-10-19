"""DEF-P1-03 / P0：进度 helper 精准单测。"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.p1

from services.evaluation_pipeline import (
    JUDGE_RUN_COUNT,
    compute_offline_progress_total,
    compute_online_progress_total,
)


@pytest.mark.parametrize(
    "questions,metrics,inference_rounds,expected",
    [
        (5, ["correctness", "faithfulness", "relevance"], 1, 5 * (1 + 3)),
        (2, ["correctness", "faithfulness"], 1, 2 * (1 + 2)),
        (0, ["correctness"], 1, 0),
        (3, [], 1, 0),
        # correctness 多轮：推理 3 轮 + 质量指标每轮 Judge 3 = 6，2 题 = 12
        (2, ["correctness"], 3, 2 * (3 + 3)),
        # runtime 指标每轮采集 + correctness 每轮 Judge：
        # 推理 2 轮 + runtime 2*1 + 质量 1*2 = 6，1 题 = 6
        (
            1,
            ["inference_latency", "correctness"],
            2,
            1 * (2 + 2 * 1 + 1 * 2),
        ),
    ],
)
def test_compute_online_progress_total(
    questions, metrics, inference_rounds, expected
):
    assert (
        compute_online_progress_total(
            questions, metrics, inference_rounds
        )
        == expected
    )


@pytest.mark.parametrize(
    "questions,metrics,expected",
    [
        (4, ["correctness", "faithfulness", "relevance"], 12),
        (2, ["correctness", "faithfulness"], 4),
        (0, ["correctness"], 0),
        (2, [], 0),
        (2, ["correctness", "inference_latency"], 2),
    ],
)
def test_compute_offline_progress_total(questions, metrics, expected):
    assert compute_offline_progress_total(questions, metrics) == expected


def test_online_includes_inference_steps_per_question():
    offline = compute_offline_progress_total(2, ["a", "b"])
    online = compute_online_progress_total(2, ["a", "b"], 1)
    assert online - offline == 2


def test_judge_run_count_is_one():
    assert JUDGE_RUN_COUNT == 1
