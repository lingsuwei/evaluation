"""跨任务 Run Diff 服务单测（S+2）。"""
from __future__ import annotations

import pytest

from services.cross_task_diff_service import pick_right_task_id

pytestmark = pytest.mark.p3


def test_pick_right_task_id_prefers_explicit():
    assert pick_right_task_id(["a", "b", "c"], "a", "c") == "c"


def test_pick_right_task_id_skips_baseline():
    assert pick_right_task_id(["a", "b"], "a", "a") == "b"


def test_pick_right_task_id_first_non_baseline():
    assert pick_right_task_id(["x", "y"], "x") == "y"
