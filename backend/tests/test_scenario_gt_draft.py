"""GT Markdown 草稿抽取（ST-2-3）。"""
from services.scenario_gt_draft_service import draft_steps_from_markdown


def test_draft_from_markdown_headings():
    md = """## 态势研判
- 海况系数 0.727
- 威胁方向 225°

## 快艇方案
- CMD_CORRIDOR_001
- 航路点闭合
"""
    out = draft_steps_from_markdown(md, scenario_key="test_scene", name="测试")
    assert out["scenario_key"] == "test_scene"
    assert len(out["steps"]) == 2
    assert out["steps"][0]["step_key"] == "S1"
    assert "0.727" in out["steps"][0]["gt_expected"]["keywords"] or "海况系数" in str(
        out["steps"][0]["gt_criteria"]
    )
