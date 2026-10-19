"""model_answer 子步骤提炼。"""
import json

from services.scenario_model_answer_draft_service import draft_steps_from_model_answer


def test_extract_numbered_chinese_list():
    ma = """1、定位ECO-001受影响对象 → 从ECO受影响对象.md
2、EPart版本映射 → 通过EPart版本
3、MPart工艺关系映射 → A001→A101"""
    out = draft_steps_from_model_answer(ma, question="问题1")
    assert len(out["steps"]) == 3
    assert out["steps"][0]["step_key"] == "N1"
    assert "ECO" in out["steps"][0]["name"] or "ECO" in str(out["steps"][0]["gt_expected"])


def test_extract_step_labels():
    ma = """根据业务逻辑：
步骤 1：第一层判断 - 检查 F000 架次
步骤 2：第二层判断 - 检查处置结论
步骤 3：获取物料成本
步骤 4：计算报废成本"""
    out = draft_steps_from_model_answer(ma)
    assert len(out["steps"]) == 4
    assert out["steps"][0]["name"].startswith("第一层")


def test_onto_eval_12_first_item_shape():
  import json
  from pathlib import Path
  for p in [
      Path(__file__).resolve().parents[2] / "datasets" / "onto-eval-20-with-context.json",
      Path(__file__).resolve().parents[3].parent / "onto-eval-12-with-context.json",
  ]:
      if not p.exists():
          continue
      data = json.loads(p.read_text(encoding="utf-8"))
      item = data[0]
      out = draft_steps_from_model_answer(
          item.get("model_answer") or "",
          question=item.get("question"),
          ground_truth=item.get("ground_truth"),
      )
      assert len(out["steps"]) >= 1
      names = [s["name"] for s in out["steps"]]
      assert not any("态势综合研判" in n for n in names)  # 非 ZC 硬编码
      return
  # 无文件时用内联
  item = {
      "question": "问题1：ECO-001",
      "model_answer": "1、定位ECO-001\n2、EPart映射\n3、MPart映射",
  }
  out = draft_steps_from_model_answer(item["model_answer"], question=item["question"])
  assert len(out["steps"]) == 3


def test_noise_section_filtered_from_model_answer():
    """噪声段（过渡性/无实质内容）应被过滤，剩余步骤权重归一化。

    复现场景：golden model_answer 中"步骤0"是过渡话术（body 仅1行指引，
    无数据标志，含"进入步骤"指引词），不是可评测的业务步骤。
    """
    ma = """### 步骤0： 结论：状态=通过，可以进入步骤1。

现在进入步骤1：任务要素提取（步骤3至5）。

### 步骤1： 命令与情报完整性校验

✅ 作战命令编号：OPORD-26-08
✅ 参考时刻：2026-08-08 08:00:00

### 步骤2： 兵力授权校验

装备类型 可用总数 授权上限 说明
AMV 3 3 两栖突击营
"""
    out = draft_steps_from_model_answer(ma, question="test", name="Q1")
    steps = out["steps"]
    # S0 被过滤，只剩 2 步
    assert len(steps) == 2
    assert not any("步骤0" in (s.get("name") or "") for s in steps)
    # 权重归一化为 0.5/0.5
    assert abs(steps[0]["weight"] - 0.5) < 0.01
    assert abs(steps[1]["weight"] - 0.5) < 0.01


def test_legitimate_short_step_not_filtered():
    """合理短步骤不应被误杀。

    body 短但含声明性内容（非过渡话术），应保留。
    """
    ma = """### 步骤1： 任务要素提取

任务类型固定为察打评。

### 步骤2： 兵力授权校验

AMV 3 3 两栖突击营

### 步骤3： 环境汇总

ENV-26-08 已查到。
"""
    out = draft_steps_from_model_answer(ma, question="test", name="Q2")
    steps = out["steps"]
    assert len(steps) == 3
    names = [s.get("name") or "" for s in steps]
    assert any("任务要素提取" in n for n in names)
    assert any("兵力授权校验" in n for n in names)
    assert any("环境汇总" in n for n in names)


def test_structural_noise_filtered_in_regex_path():
    """正则提取路径过滤结构性噪声关键词（步骤标题/过渡话术/纯通用过程词）。

    根因修复：_sections_to_steps / draft_steps_from_markdown 之前只过滤
    is_specific_value_keyword，未过滤 is_structural_noise_keyword，导致
    步骤标题（"步骤1：命令与情报完整性校验"）混入 output_keywords，
    agent 输出永不含这些原文 → 系统性 0 命中。

    场景：GT body 长句（步骤标题被 _KEYWORD_INLINE_RE 抽到前 24 字），
    应被 is_structural_noise_keyword 过滤掉，只保留有辨识力的术语。
    """
    ma = """### 步骤1： 命令与情报完整性校验

作战命令编号 OPORD-26-08。

### 步骤2： 兵力授权校验

AMV 可用总数 3 两栖突击营。
"""
    out = draft_steps_from_model_answer(ma, question="test", name="Q3")
    steps = out["steps"]
    assert len(steps) == 2

    s1_keywords = steps[0]["gt_expected"]["keywords"]
    s1_binding_keywords = steps[0]["match_rules"]["trace_binding"]["output_keywords"]
    # 步骤标题（"步骤1：命令与情报完整性校验"）应被过滤
    for kw in s1_keywords:
        assert not kw.startswith("步骤1"), f"步骤标题未被过滤: {kw}"
        assert not kw.startswith("第一步"), f"步骤标题未被过滤: {kw}"
    for kw in s1_binding_keywords:
        assert not kw.startswith("步骤1"), f"步骤标题未在 output_keywords 过滤: {kw}"
    # 有辨识力的术语保留
    assert any("OPORD" in kw or "作战命令" in kw for kw in s1_keywords)


def test_structural_noise_filtered_in_gt_draft_path():
    """GT Markdown 草稿路径同样过滤结构性噪声关键词。"""
    from services.scenario_gt_draft_service import draft_steps_from_markdown

    md = """# 步骤1： 命令与情报完整性校验

- 作战命令编号 OPORD-26-08

# 步骤2： 兵力授权校验

- AMV 可用总数 3 两栖突击营
"""
    out = draft_steps_from_markdown(md)
    steps = out["steps"]
    assert len(steps) == 2

    for step in steps:
        for kw in step["gt_expected"]["keywords"]:
            assert not kw.startswith("步骤"), f"GT 草稿步骤标题未被过滤: {kw}"
            assert not kw.startswith("第一步"), f"GT 草稿步骤标题未被过滤: {kw}"


# ── LLM 关键词提取测试 ──

def test_is_structural_noise_keyword():
    """结构性噪声关键词过滤（通用形态，非领域词表）。"""
    from services.scenario_gt_draft_service import is_structural_noise_keyword

    # 步骤编号/标题前缀
    assert is_structural_noise_keyword("步骤1")
    assert is_structural_noise_keyword("第一步")
    assert is_structural_noise_keyword("SP1")
    assert is_structural_noise_keyword("步骤1：任务要素提取")
    # 过渡话术
    assert is_structural_noise_keyword("进入下一步")
    assert is_structural_noise_keyword("开始执行")
    # 纯通用过程词
    assert is_structural_noise_keyword("步骤")
    assert is_structural_noise_keyword("分析")
    assert is_structural_noise_keyword("校验")
    # 过短
    assert is_structural_noise_keyword("A")
    assert is_structural_noise_keyword("")

    # 非噪声（有辨识力的领域短语）
    assert not is_structural_noise_keyword("察打评")
    assert not is_structural_noise_keyword("两栖突击营")
    assert not is_structural_noise_keyword("授权上限")
    assert not is_structural_noise_keyword("ENV-26-08")


def test_parse_keyword_json():
    """容错解析 LLM 返回的关键词 JSON。"""
    from services.scenario_model_answer_draft_service import _parse_keyword_json

    # 标准 JSON
    m = _parse_keyword_json('[{"step_key":"S1","keywords":["a","b"]}]')
    assert m == {"S1": ["a", "b"]}

    # 带前后解释文字
    m = _parse_keyword_json('好的，结果如下：\n[{"step_key":"S2","keywords":["x"]}]\n以上。')
    assert m == {"S2": ["x"]}

    # 空响应
    assert _parse_keyword_json("") == {}
    assert _parse_keyword_json("无 JSON") == {}

    # 非法 JSON
    assert _parse_keyword_json("[broken") == {}


async def test_enhance_step_keywords_with_llm_overrides():
    """LLM 关键词覆盖正则关键词，非正文子串的词被过滤。"""
    from unittest.mock import AsyncMock, patch

    from services.scenario_model_answer_draft_service import enhance_step_keywords_with_llm

    steps = [
        {
            "step_key": "S1",
            "name": "步骤1：任务要素提取",
            "gt_criteria": ["任务类型固定为察打评"],
            "gt_expected": {"keywords": ["察打评"], "text": "任务类型固定为察打评。"},
            "match_rules": {
                "trace_binding": {
                    "span_name_patterns": ["llm:main_iteration", "tool:"],
                    "output_keywords": ["察打评"],
                }
            },
        },
        {
            "step_key": "S2",
            "name": "步骤2：兵力授权校验",
            "gt_criteria": ["AMV 可用总数 3 授权上限 3 两栖突击营"],
            "gt_expected": {
                "keywords": ["AMV"],
                "text": "AMV 可用总数 3 授权上限 3 两栖突击营",
            },
            "match_rules": {
                "trace_binding": {
                    "span_name_patterns": ["llm:main_iteration", "tool:"],
                    "output_keywords": ["AMV"],
                }
            },
        },
    ]

    # LLM 返回：含正文子串、步骤编号（应过滤）、非正文词（应过滤）、具体值（应过滤）
    llm_resp = json.dumps([
        {"step_key": "S1", "keywords": ["察打评", "任务类型", "步骤1", "不存在的词"]},
        {"step_key": "S2", "keywords": ["两栖突击营", "授权上限", "兵力授权", "3架"]},
    ])

    with patch(
        "evaluators.llm_client.evaluate_with_llm",
        new_callable=AsyncMock,
        return_value=llm_resp,
    ):
        result = await enhance_step_keywords_with_llm(steps)

    s1 = result[0]
    # "步骤1" 被结构性过滤，"不存在的词" 非正文子串被过滤
    assert "察打评" in s1["gt_expected"]["keywords"]
    assert "任务类型" in s1["gt_expected"]["keywords"]
    assert "步骤1" not in s1["gt_expected"]["keywords"]
    assert "不存在的词" not in s1["gt_expected"]["keywords"]
    assert s1["match_rules"]["trace_binding"]["output_keywords"][:2] == ["察打评", "任务类型"]

    s2 = result[1]
    # "兵力授权" 非正文子串被过滤，"3架" 具体值被过滤
    assert "两栖突击营" in s2["gt_expected"]["keywords"]
    assert "授权上限" in s2["gt_expected"]["keywords"]
    assert "兵力授权" not in s2["gt_expected"]["keywords"]
    assert "3架" not in s2["gt_expected"]["keywords"]


async def test_enhance_falls_back_on_llm_failure():
    """LLM 失败时回退到正则关键词（保持原值）。"""
    from unittest.mock import AsyncMock, patch

    from services.scenario_model_answer_draft_service import enhance_step_keywords_with_llm

    steps = [
        {
            "step_key": "S1",
            "name": "步骤1",
            "gt_criteria": ["内容A"],
            "gt_expected": {"keywords": ["正则词A"], "text": "内容A"},
            "match_rules": {"trace_binding": {"output_keywords": ["正则词A"]}},
        },
        {
            "step_key": "S2",
            "name": "步骤2",
            "gt_criteria": ["内容B"],
            "gt_expected": {"keywords": ["正则词B"], "text": "内容B"},
            "match_rules": {"trace_binding": {"output_keywords": ["正则词B"]}},
        },
    ]

    with patch(
        "evaluators.llm_client.evaluate_with_llm",
        new_callable=AsyncMock,
        side_effect=RuntimeError("LLM 不可用"),
    ):
        result = await enhance_step_keywords_with_llm(steps)

    # 保持原值不变
    assert result[0]["gt_expected"]["keywords"] == ["正则词A"]
    assert result[1]["gt_expected"]["keywords"] == ["正则词B"]


async def test_enhance_skips_single_step():
    """单步无需跨步骤辨识，直接返回。"""
    from services.scenario_model_answer_draft_service import enhance_step_keywords_with_llm

    steps = [{"step_key": "S1", "name": "单步", "gt_expected": {"keywords": ["k"]}}]
    result = await enhance_step_keywords_with_llm(steps)
    assert result is steps
