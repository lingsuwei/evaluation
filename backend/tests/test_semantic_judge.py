"""P5 semantic_judge 单测。"""
from __future__ import annotations

import pytest

from services.diff.semantic_judge import build_rule_based_judge_summary

pytestmark = pytest.mark.p5


def test_judge_summary_all_aligned():
    text = build_rule_based_judge_summary(
        {"align_aggregate_keys": ["step:plan"], "role_mismatch_keys": [], "summary_mismatch_keys": []}
    )
    assert "一致" in text


def test_judge_summary_mismatches():
    text = build_rule_based_judge_summary(
        {
            "role_mismatch_keys": ["step:plan"],
            "summary_mismatch_keys": ["tool:x"],
            "only_left_aggregate_keys": ["tool:search"],
            "only_right_aggregate_keys": [],
            "align_aggregate_keys": [],
        }
    )
    assert "角色不一致" in text
    assert "摘要不一致" in text
    assert "仅左 Run" in text
