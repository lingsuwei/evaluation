"""场景 QA 子步骤评测单测（P1-C/D）。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, EvaluationResult, ScenarioEvalStep, ScenarioStepResult
from services.eval_context import build_eval_context
from services.scenario_step_trace_service import bind_step_observation
from services.scenario_eval_service import (
    GENERIC_L0_SCENARIO_KEY,
    GENERIC_L0_STEPS,
    ensure_generic_l0_template,
    get_template_by_key,
    score_run_against_scenario,
    score_task_scenario,
)

pytestmark = pytest.mark.p3


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


GENERIC_ANSWER = "理解问题 调用工具推理 综上答案结论"


async def _seed_generic_run(session: AsyncSession):
    started = datetime.utcnow()
    session.add(
        AgentRun(
            id="run_generic",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t_scen", dataset_id="d1", question_id=0, run_index=0
            ),
            task_id="t_scen",
            dataset_id="d1",
            question_id=0,
            agent_id="ag1",
            agent_name="Agent",
            status="success",
            started_at=started,
            agent_snapshot="{}",
        )
    )
    session.add(
        EvaluationResult(
            task_id="t_scen",
            question_id=0,
            model="m",
            metric_name="correctness",
            score=0.8,
            avg_score=0.8,
            passed=True,
            agent_run_id="run_generic",
            question="测试问题",
            ground_truth="",
            agent_answer=GENERIC_ANSWER,
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_ensure_generic_l0_template(mem_session):
    tpl = await ensure_generic_l0_template(mem_session)
    assert tpl.scenario_key == GENERIC_L0_SCENARIO_KEY
    detail = await get_template_by_key(mem_session, GENERIC_L0_SCENARIO_KEY)
    assert detail is not None
    assert len(detail["steps"]) == len(GENERIC_L0_STEPS)


@pytest.mark.asyncio
async def test_score_run_against_generic_l0(mem_session):
    await _seed_generic_run(mem_session)
    report = await score_run_against_scenario(
        mem_session, "run_generic", GENERIC_L0_SCENARIO_KEY, persist=True
    )
    assert report is not None
    assert report["composite_score"] > 0
    assert len(report["steps"]) == 3
    assert any(s["passed"] for s in report["steps"])


@pytest.mark.asyncio
async def test_score_run_skipped_when_scenario_disabled(mem_session):
    """auto_scenario_score=False → score_run_against_scenario 返回 None，不补算。"""
    from database import EvaluationTask

    await _seed_generic_run(mem_session)
    mem_session.add(
        EvaluationTask(
            id="t_scen",
            dataset_id="d1",
            models='["glm"]',
            status="completed",
            auto_scenario_score=False,
        )
    )
    await mem_session.flush()
    report = await score_run_against_scenario(
        mem_session, "run_generic", GENERIC_L0_SCENARIO_KEY, persist=True
    )
    assert report is None


@pytest.mark.asyncio
async def test_required_cmd_ids_scoring():
    from services.scenario_eval_service import _score_step

    cmd_ids = [f"CMD_{i:03d}" for i in range(1, 9)]
    all_eight = " ".join(cmd_ids) + " 航路点闭合"
    score, passed, ev = _score_step(
        all_eight,
        {"required_cmd_ids": cmd_ids, "keywords": ["航路点"]},
    )
    assert passed
    assert score == 1.0
    assert not ev.get("missing_cmd_ids")

    partial = "CMD_001 CMD_008 航路点"
    score2, passed2, ev2 = _score_step(
        partial,
        {"required_cmd_ids": cmd_ids, "keywords": ["航路点"]},
    )
    assert not passed2
    assert score2 == 0.25
    assert len(ev2["matched_cmd_ids"]) == 2
    assert len(ev2["missing_cmd_ids"]) == 6


@pytest.mark.asyncio
async def test_score_task_scenario(mem_session):
    await _seed_generic_run(mem_session)
    report = await score_task_scenario(mem_session, "t_scen", GENERIC_L0_SCENARIO_KEY)
    assert report["task_id"] == "t_scen"
    assert len(report["runs"]) == 1


@pytest.mark.asyncio
async def test_scoring_ignores_ground_truth_only(mem_session):
    """GT 含关键词但 agent 答案为空时，不得因 GT 给子步骤加分。"""
    started = datetime.utcnow()
    mem_session.add(
        AgentRun(
            id="run_gt_only",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t_gt", dataset_id="d1", question_id=0, run_index=0
            ),
            task_id="t_gt",
            dataset_id="d1",
            question_id=0,
            agent_id="ag1",
            status="success",
            started_at=started,
            agent_snapshot="{}",
        )
    )
    mem_session.add(
        EvaluationResult(
            task_id="t_gt",
            question_id=0,
            model="m",
            metric_name="correctness",
            score=0.0,
            avg_score=0.0,
            passed=False,
            agent_run_id="run_gt_only",
            question="测试",
            ground_truth="答案 结论 综上",
            agent_answer="",
        )
    )
    await mem_session.flush()
    report = await score_run_against_scenario(
        mem_session, "run_gt_only", GENERIC_L0_SCENARIO_KEY, persist=False
    )
    l03 = next(s for s in report["steps"] if s["step_key"] == "L0-3")
    assert l03["score"] == 0.0
    assert not l03["passed"]


@pytest.mark.asyncio
async def test_build_run_steps_detail_recomputes_composite_from_persisted(mem_session):
    """持久化分数覆盖后，composite_score 须按持久化分数重算，
    而非保留 skip_llm_judge 路径下 keyword 模式的陈旧值。"""
    from sqlalchemy import select

    from services.scenario_step_trace_service import build_run_scenario_steps_detail
    from tests.scenario_test_helpers import seed_item_template

    sk = await seed_item_template(mem_session)
    # seed_item_template 创建 S1/S2 两步，关键词分别为 [拦截,规划] / [航路点,CMD]
    # agent_answer 故意不含任何关键词 → keyword 模式两步均 0 分
    started = datetime.utcnow()
    mem_session.add(
        AgentRun(
            id="run_persist",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t_persist",
                dataset_id="ds_sc",
                question_id=0,
                scenario_key=sk,
            ),
            task_id="t_persist",
            dataset_id="ds_sc",
            question_id=0,
            agent_id="ag_s1",
            status="success",
            started_at=started,
            agent_snapshot="{}",
        )
    )
    mem_session.add(
        EvaluationResult(
            task_id="t_persist",
            question_id=0,
            model="m",
            metric_name="correctness",
            score=0.5,
            avg_score=0.5,
            passed=True,
            agent_run_id="run_persist",
            question="测试",
            ground_truth="",
            agent_answer="完全无关的内容，不含任何关键词",
        )
    )
    # 持久化高分结果（模拟 llm_judge 评分）
    steps = (
        await mem_session.execute(
            select(ScenarioEvalStep).where(ScenarioEvalStep.step_key.in_(["S1", "S2"]))
        )
    ).scalars().all()
    assert len(steps) == 2
    step_by_key = {s.step_key: s for s in steps}
    mem_session.add_all(
        [
            ScenarioStepResult(
                id="ssr_s1",
                agent_run_id="run_persist",
                step_id=step_by_key["S1"].id,
                score=0.9,
                passed=True,
                evidence={"score_source": "llm_judge"},
            ),
            ScenarioStepResult(
                id="ssr_s2",
                agent_run_id="run_persist",
                step_id=step_by_key["S2"].id,
                score=0.8,
                passed=True,
                evidence={"score_source": "llm_judge"},
            ),
        ]
    )
    await mem_session.flush()

    detail = await build_run_scenario_steps_detail(mem_session, "run_persist")
    assert detail is not None
    # 期望: (0.9*0.5 + 0.8*0.5) / 1.0 = 0.85
    assert abs(detail["composite_score"] - 0.85) < 0.01
    s1 = next(s for s in detail["steps"] if s["step_key"] == "S1")
    s2 = next(s for s in detail["steps"] if s["step_key"] == "S2")
    assert s1["score"] == 0.9
    assert s2["score"] == 0.8
    # weighted_score 也应随持久化分数同步
    assert abs(s1["weighted_score"] - 0.45) < 0.01
    assert abs(s2["weighted_score"] - 0.4) < 0.01


def test_bind_step_observation_step_index_without_keywords_falls_through():
    """step_index 精确命中但关键词未命中时，不得短路绑定到无关 span。

    复现 run_span_nodes 路径的错配：span 树 DFS step_index 恰好等于
    场景步骤序号，但 span 内容是 reasoning_summary / 状态摘要（无关键词），
    此时必须降级到 heuristic，绑到关键词命中的真实内容 span。
    """
    keywords = ["兵力授权", "AMV"]
    # obs_a: step_index 命中（=2），但内容是 token 统计摘要，无关键词
    obs_a = {
        "id": "obs_reasoning_summary",
        "name": "llm:reasoning_summary",
        "type": "GENERATION",
        "input": {"step_index": 2},
        "output": "token_usage: input=1234 output=567 total=1801",
        "metadata": {"step_index": 2},
    }
    # obs_b: step_index 不匹配（=7），但内容含关键词
    obs_b = {
        "id": "obs_main_iter_7",
        "name": "llm:main_iteration",
        "type": "GENERATION",
        "input": {"step_index": 7},
        "output": "兵力授权校验：AMV 可用 3 辆，授权上限 3，校验通过",
        "metadata": {"step_index": 7},
    }
    observations = [obs_a, obs_b]
    binding = {
        "span_name_patterns": ["llm:main_iteration", "llm:main", "tool:"],
        "output_keywords": keywords,
        "expected_step_index": 2,  # 显式声明（模拟 run_span_nodes 路径的自动设值）
        "label": "兵力授权校验",
    }
    bound, meta = bind_step_observation(
        "S2", observations, binding=binding,
    )
    # 不能绑到 obs_a（step_index 命中但无关键词）
    assert bound is not None
    assert bound["id"] == "obs_main_iter_7"
    assert meta["bind_source"] != "step_index"
    assert "兵力授权" in (meta.get("matched_keywords") or [])


def test_bind_step_observation_step_index_with_keywords_short_circuits():
    """step_index 精确命中且关键词也命中时，仍可短路返回（保留显式配置的能力）。"""
    obs = {
        "id": "obs_verify",
        "name": "tool:verify_authorization",
        "type": "TOOL",
        "input": {"step_index": 2},
        "output": "兵力授权 AMV 校验通过，授权上限 3 辆",
        "metadata": {"step_index": 2},
    }
    binding = {
        "span_name_patterns": ["tool:"],
        "output_keywords": ["兵力授权", "AMV"],
        "expected_step_index": 2,
        "label": "兵力授权校验",
    }
    bound, meta = bind_step_observation(
        "S2", [obs], binding=binding,
    )
    assert bound is not None
    assert bound["id"] == "obs_verify"
    assert meta["bind_source"] == "step_index"


def test_keyword_symbol_normalization_match():
    """改动1: keyword 符号归一化宽松匹配。

    agent 输出格式与 golden keyword 存在标点/空格差异时，应宽松匹配命中。
    """
    from services.scenario_eval_service import _keyword_hit, _normalize_text

    text = "LM-01: 1发（WPN-HEAT），LM-02: 1发（WPN-HEAT）"
    norm = _normalize_text(text)

    # 精确匹配
    assert _keyword_hit(norm, "WPN")
    assert _keyword_hit(norm, "HEAT")

    # 符号差异宽松匹配
    assert _keyword_hit(norm, "LM-01：1发"), "全角冒号应宽松匹配"
    assert _keyword_hit(norm, "LM01 1发"), "去符号后应宽松匹配"

    # 不应误命中
    assert not _keyword_hit(norm, "XYZ")


def test_specific_value_keyword_filtered():
    """改动2: 具体值型关键词应被过滤。

    golden answer 的具体数值/公式结果（如 LM-01:1发、状态=已确认）
    不应作为 keyword 模式判分点。
    """
    from services.scenario_gt_draft_service import is_specific_value_keyword

    # 具体值型（应过滤）
    assert is_specific_value_keyword("LM-01:1发（WPN-HEAT）")
    assert is_specific_value_keyword("侦察引导(主选)：所需数量=3架，已选3架✅")
    assert is_specific_value_keyword("所有需求项均满足，状态=已确认。")
    assert is_specific_value_keyword("打击能力总和=2发 ≥ 2发 ✅")

    # 抽象标识符型（应保留）
    assert not is_specific_value_keyword("WPN")
    assert not is_specific_value_keyword("HEAT")
    assert not is_specific_value_keyword("AMV")
    assert not is_specific_value_keyword("步骤6：歼灭保障和满足性校验")
    assert not is_specific_value_keyword("ENV")


def test_ontology_context_output_downweighted_when_input_key_no_match():
    """改进1：ontology_context 的 output 关键词命中应降权，当 input.key 与步骤名不匹配时不应抢绑定。

    复现 trace 0489e8ee S5 错配：e40e5f7e 的 input.key=任务要素提取，
    但 S5 步骤名是"环境、时限和授权要素汇总"，input.key 不匹配。
    S5 关键词"环境""ENV"在规则文档 output 里命中，但语义上那是规则定义。
    应绑到 business_data_search 的环境态势查询（input.entity=环境态势）。
    """
    # 规则文档 span（output 含多个关键词但 input.key 不匹配步骤）
    obs_rule = {
        "id": "e40e5f7e",
        "name": "tool:ontology_context",
        "type": "TOOL",
        "input": {"key": "任务要素提取", "ontology_id": "x", "resource": "operation"},
        "output": "operation_key: 任务要素提取\n环境态势提取规则\n威胁要素\n时限要素\n授权要素 ENV",
        "metadata": {"iteration": 3},
    }
    # 数据查询 span（input.entity 匹配，output 较短）
    obs_data = {
        "id": "4874858c",
        "name": "tool:business_data_search",
        "type": "TOOL",
        "input": {"entity": "环境态势", "query": "查询环境态势ENV-26-08"},
        "output": "ENV-26-08 地形沙质 风速5 能见度8",
        "metadata": {"iteration": 3},
    }
    binding = {
        "span_name_patterns": ["llm:main_iteration", "tool:"],
        "output_keywords": ["环境", "ENV", "环境态势"],
        "label": "步骤5： 环境、时限和授权要素汇总",
    }
    bound, meta = bind_step_observation("S5", [obs_rule, obs_data], binding=binding)
    assert bound is not None, "应绑定到数据查询 span"
    assert bound["id"] == "4874858c", f"应绑到环境态势查询，实际 {bound.get('id')}"
    assert meta["bind_source"] == "heuristic"
    assert meta.get("tool_kind") == "data_search"
    assert meta.get("input_score", 0) > 0, "data_search input 语义应得分"


def test_business_data_search_entity_match_preferred():
    """改进1：business_data_search 的 input.entity/query 命中关键词时应优先绑定。

    复现 trace 049e8ee S4：应绑到目标查询（含 T-001 威胁等级），
    而非地面装备状态查询（output 更长但语义无关）。
    """
    # 目标查询（含威胁等级，语义对应"潜在威胁提取"）
    obs_target = {
        "id": "771cedbe",
        "name": "tool:business_data_search",
        "type": "TOOL",
        "input": {"entity": "目标", "query": "查询目标T-001档案威胁等级"},
        "output": "T-001 自行火炮 ThreatLevel高 机动中",
        "metadata": {"iteration": 6},
    }
    # 地面装备查询（output 更长，但语义无关）
    obs_ground = {
        "id": "8b665311",
        "name": "tool:business_data_search",
        "type": "TOOL",
        "input": {"entity": "地面装备", "query": "查询DOG WRV UGV状态"},
        "output": "DOG-01 正常 待命\nWRV-01 正常 待命\nUGV-01 正常 待命 通信正常 燃油96",
        "metadata": {"iteration": 12},
    }
    binding = {
        "span_name_patterns": ["llm:main_iteration", "tool:"],
        "output_keywords": ["潜在威胁", "T-001", "威胁等级"],
        "label": "步骤4： 潜在威胁提取",
    }
    bound, meta = bind_step_observation("S4", [obs_target, obs_ground], binding=binding)
    assert bound is not None
    assert bound["id"] == "771cedbe", f"应绑到目标查询，实际 {bound.get('id')}"
    assert meta.get("tool_kind") == "data_search"
    assert meta.get("input_score", 0) > 0


def test_llm_output_keywords_preferred_over_ontology_input_key():
    """分层信号：output 关键词命中（PRIMARY）优先于 input 语义匹配（SECONDARY）。

    ontology_context 的 input.key 与步骤名强匹配（input_score=20），
    但其 output 是规则文档（降权 0.3，2个kw命中 → 0.6 分）。
    LLM span 的 output 有 2个kw命中（不降权 → 2.0 分）。
    LLM 的实际执行输出比规则文档定义是更好的 evidence，
    PRIMARY 得分高者应胜出，input_score 仅在 PRIMARY 相同时作 tiebreaker。
    """
    # ontology_context 查"装备选型"规则（input.key 匹配步骤名，但 output 是规则定义）
    obs_rule = {
        "id": "26fa7080",
        "name": "tool:ontology_context",
        "type": "TOOL",
        "input": {"key": "装备选型", "resource": "operation"},
        "output": "operation_key: 装备选型\n业务描述：候选查询硬过滤评分",
        "metadata": {"iteration": 9},
    }
    # LLM span output 命中更多关键词（实际执行输出）
    obs_other = {
        "id": "other_span",
        "name": "llm:main_iteration",
        "type": "GENERATION",
        "input": {},
        "output": "装备选型校验：装备可用 装备能力匹配 弹药充足 装备选型完成",
        "metadata": {"iteration": 10},
    }
    binding = {
        "span_name_patterns": ["llm:main_iteration", "tool:"],
        "output_keywords": ["装备选型", "装备"],
        "label": "步骤7： 装备选型校验",
    }
    bound, meta = bind_step_observation("S7", [obs_rule, obs_other], binding=binding)
    assert bound is not None
    assert bound["id"] == "other_span", f"LLM output kw 命中更多应胜出，实际 {bound.get('id')}"
    assert meta.get("input_score", 0) == 0, "LLM span 无 input 语义匹配"


def test_ontology_input_key_as_tiebreaker_when_kw_equal():
    """input_score 作 tiebreaker：output kw 命中相同时，input 语义匹配强的优先。

    两个 span 都命中 1 个 output kw（PRIMARY 相同），
    有 input 语义匹配的 span 应在 tiebreaker 中胜出。
    """
    # data_search: input.entity 命中 + output 有 1 个 kw
    obs_data = {
        "id": "data_span",
        "name": "tool:business_data_search",
        "type": "TOOL",
        "input": {"entity": "目标", "query": "查询目标T-001威胁等级"},
        "output": "T-001 威胁等级高",
        "metadata": {"iteration": 5},
    }
    # LLM: 无 input 语义 + output 有 1 个 kw
    obs_llm = {
        "id": "llm_span",
        "name": "llm:main_iteration",
        "type": "GENERATION",
        "input": {},
        "output": "威胁等级评估完成",
        "metadata": {"iteration": 6},
    }
    binding = {
        "span_name_patterns": ["llm:main_iteration", "tool:"],
        "output_keywords": ["威胁等级"],
        "label": "步骤4： 威胁评估",
    }
    bound, meta = bind_step_observation("S4", [obs_data, obs_llm], binding=binding)
    assert bound is not None
    assert bound["id"] == "data_span", f"kw 相同时 input 语义匹配应作 tiebreaker 胜出，实际 {bound.get('id')}"
    assert meta.get("input_score", 0) > 0


def test_score_zero_uses_final_answer_but_keeps_obs_id():
    """score=0 + 有终局答案 → 用终局答案评测，但保留 best span 的 observation_id 供调试。

    onto-agent 并行处理：步骤 evidence 可能跨多个 span 或仅在终局答案中，
    单个 span 0 命中关键词时，用终局答案评测比用无关 span output 更准确。
    但应保留 best span 的 observation_id（不清空），供 span_refs 记录、
    trace 深链生成和 used_obs_ids 去重。
    """
    # 无关 span（output 不含任何关键词）
    obs_irrelevant = {
        "id": "8b665311",
        "name": "tool:business_data_search",
        "type": "TOOL",
        "input": {"entity": "地面装备", "query": "查询DOG状态"},
        "output": "DOG-01 正常 待命 通信正常 燃油96",
        "metadata": {"iteration": 12},
    }
    final_answer = "步骤8：方案汇总 临机云方案构建完成，装备选型结果如下..."
    bound, meta = bind_step_observation(
        "S8",
        [obs_irrelevant],
        binding={
            "span_name_patterns": ["llm:main_iteration", "tool:"],
            "output_keywords": ["方案汇总", "临机云方案"],
            "label": "步骤8： 方案汇总",
        },
        final_answer_text=final_answer,
        final_answer_steps=frozenset({"S8"}),
    )
    assert bound is None, "score=0 + 有终局答案 → return None，用终局答案评测"
    assert meta["bind_source"] == "final_answer_no_span"
    assert meta.get("observation_id") == "8b665311", "应保留 best span 的 observation_id 供调试"
    assert meta.get("fallback_reason") == "no_keyword_match_use_final_answer"


def test_fallback_keywords_from_title_and_entity():
    """改进3：LLM 提取失败时兜底关键词从步骤名拆词 + 正文实体编号。

    S4 标准答案极短（仅一句话），LLM 可能提取不出有效关键词。
    兜底应从标题"步骤4： 潜在威胁提取"拆出"潜在威胁提取"，
    从正文提取实体编号 INTEL / T-00 等高辨识力词。
    """
    from services.scenario_model_answer_draft_service import _extract_fallback_keywords

    kws = _extract_fallback_keywords(
        "步骤4： 潜在威胁提取",
        "敌情情报INTEL-26-08-01关联目标为T-001（非空），所以无潜在威胁情报。",
    )
    # 应包含标题核心词"潜在威胁提取"
    assert any("潜在威胁" in kw for kw in kws), f"应含标题核心词，实际 {kws}"
    # 应包含实体编号（INTEL 或 T-00）
    assert any("INTEL" in kw or "T-0" in kw for kw in kws), f"应含实体编号，实际 {kws}"
    # 不应包含步骤编号前缀
    assert not any(kw.startswith("步骤") for kw in kws), f"不应含步骤前缀，实际 {kws}"
