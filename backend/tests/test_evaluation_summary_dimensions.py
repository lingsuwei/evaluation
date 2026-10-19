"""评测总结与四维结论集成单测。"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services.task_dimension_verdict_service import format_dimension_verdicts_for_summary


def test_format_dimension_verdicts_for_summary_online():
    payload = {
        "quality": {
            "status": "pass",
            "score": 0.85,
            "headline": "质量总分 0.85；忠实度 通过率 90%",
        },
        "process_consistency": {
            "status": "warn",
            "headline": "2/3 题过程全绿；1 题有差异",
        },
        "scenario_delivery": {
            "status": "pass",
            "score": 0.82,
            "headline": "场景综合 0.82；必做步骤 8/8 通过",
        },
        "runtime_stability": {
            "headline": "结构：各轮拓扑一致；性能：时延 87% 达标",
            "structural": {
                "status": "consistent",
                "headline": "各轮 step 拓扑一致（3 题多轮）",
            },
            "performance": {
                "status": "within_target",
                "headline": "时延 87% 达标、Token 93% 达标",
            },
        },
    }
    text = format_dimension_verdicts_for_summary(payload)
    assert "质量指标 [达标]" in text
    assert "过程一致性 [需关注]" in text
    assert "场景子步骤交付 [达标]" in text
    assert "运行稳定性" in text
    assert "结构 [" not in text
    assert "性能 [性能达标]" in text


def test_format_dimension_verdicts_for_summary_offline():
    payload = {
        "quality": {"status": "pass", "score": 0.9, "headline": "质量总分 0.90"},
        "process_consistency": {"status": "na", "headline": "离线任务不含过程一致性评测"},
        "scenario_delivery": {"status": "na", "headline": "离线任务不含场景子步骤评测"},
        "runtime_stability": {
            "headline": "离线任务不含运行稳定性评测",
            "structural": {"status": "na", "headline": "离线任务不适用"},
            "performance": {"status": "na", "headline": "离线任务不适用"},
        },
    }
    text = format_dimension_verdicts_for_summary(payload)
    assert "过程一致性：不适用" in text
    assert "场景子步骤交付：不适用" in text
    assert "运行稳定性：不适用" in text


@pytest.mark.asyncio
async def test_generate_evaluation_summary_includes_dimension_block():
    from evaluators.llm_client import generate_evaluation_summary

    dimension_verdicts = {
        "quality": {"status": "pass", "score": 0.8, "headline": "质量良好"},
        "process_consistency": {"status": "pass", "headline": "3/3 题全绿"},
        "scenario_delivery": {"status": "na", "headline": "未配置"},
        "runtime_stability": {
            "headline": "结构一致；性能达标",
            "structural": {"status": "consistent", "headline": "拓扑一致"},
            "performance": {"status": "within_target", "headline": "时延达标"},
        },
    }

    captured: dict = {}

    async def fake_llm(prompt, **kwargs):
        captured["prompt"] = prompt
        return "mock summary"

    with patch("evaluators.llm_client.evaluate_with_llm", new=AsyncMock(side_effect=fake_llm)):
        summary = await generate_evaluation_summary(
            task_id="t1",
            dataset_id="ds1",
            model_name="Agent",
            results=[
                {
                    "question_id": 0,
                    "metric_name": "faithfulness",
                    "score": 0.9,
                    "passed": True,
                }
            ],
            dimension_verdicts=dimension_verdicts,
        )

    assert summary == "mock summary"
    assert "四维评测结论" in captured["prompt"]
    assert "过程一致性 [达标]" in captured["prompt"]
    assert "inference_latency" not in captured["prompt"]
