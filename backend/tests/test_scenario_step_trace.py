"""场景子步骤 Trace 绑定。"""
from services.scenario_step_trace_service import (
    bind_step_observation,
    build_langfuse_trace_urls,
    extract_observation_io,
)


def _obs(oid: str, name: str, output: str, iteration: int | None = None):
    meta = {"iteration": iteration} if iteration is not None else {}
    return {
        "id": oid,
        "name": name,
        "type": "GENERATION",
        "input": {"user_query": "针对带状防御区域…"},
        "output": output,
        "metadata": meta,
    }


def _s1_binding():
    return {
        "span_name_patterns": ["tool:ontology_context", "llm:main_iteration", "llm:main"],
        "iteration_lte": 2,
        "iteration_lte_llm_only": True,
        "output_keywords": ["海况", "0.727", "225", "B001", "B008", "威胁"],
        "label": "态势综合研判",
    }


def _s7_binding():
    return {
        "span_name_patterns": ["llm:main_iteration", "llm:reasoning_summary", "tool:geo_calc"],
        "output_keywords": ["PLAN_CORRIDOR_001", "144.99", "13.41", "扇面", "四角"],
        "label": "大船任务方案",
    }


def test_bind_s1_picks_early_llm_with_keywords():
    observations = [
        _obs("o1", "llm:main_iteration", "海况系数 0.727，威胁方向 225°，快艇 B001-B008", 1),
        _obs("o2", "tool:ontology_context", "{}", 1),
        _obs("o3", "llm:main_iteration", "后续规划…", 3),
    ]
    obs, meta = bind_step_observation("S1", observations, binding=_s1_binding())
    assert obs is not None
    assert obs["id"] == "o1"
    assert meta["bind_source"] == "heuristic"


def test_s7_flex_latitude_and_sector_keywords():
    from services.scenario_eval_service import _score_step

    text = "PLAN_CORRIDOR_001 center 144.990 13.410 防御扇面四角坐标"
    score, passed, ev = _score_step(
        text,
        {
            "keywords": ["PLAN_CORRIDOR_001", "144.99", "13.41", "扇面"],
            "alt_keywords": ["四角", "防御扇面"],
        },
    )
    assert score >= 0.75
    assert passed
    assert "13.41" in ev["matched_keywords"] or "144.99" in ev["matched_keywords"]


def test_s7_span_supplement_final_answer():
    from services.scenario_eval_service import _score_step
    from services.scenario_step_trace_service import (
        _resolve_step_score_text,
        extract_observation_io,
    )

    obs = _obs("o7", "llm:main_iteration", "PLAN_CORRIDOR_001 经度144.99", 5)
    io = extract_observation_io(obs)
    span_text, _ = _resolve_step_score_text(
        "S7", io, {"bind_source": "heuristic"}, agent_answer=""
    )
    score_span, passed_span, _ = _score_step(
        span_text,
        {
            "keywords": ["PLAN_CORRIDOR_001", "144.99", "13.41", "扇面"],
            "alt_keywords": ["四角"],
        },
    )
    assert not passed_span
    final = "PLAN_CORRIDOR_001 中心144.99,13.41 防御扇面四角"
    merged = f"{span_text}\n{final}"
    score_m, passed_m, ev = _score_step(
        merged,
        {
            "keywords": ["PLAN_CORRIDOR_001", "144.99", "13.41", "扇面"],
            "alt_keywords": ["四角"],
        },
    )
    assert passed_m
    assert score_m > score_span
    assert "13.41" in ev["matched_keywords"]


def test_bind_s7_final_answer_fallback():
    """无可用 span 时，汇总步骤可走终局答案兜底。"""
    observations: list = []
    final = "方案 PLAN_CORRIDOR_001 中心 144.99, 13.41 防御扇面四角"
    obs, meta = bind_step_observation(
        "S7",
        observations,
        binding=_s7_binding(),
        final_answer_text=final,
        final_answer_steps=frozenset({"S7"}),
    )
    assert obs is None
    assert meta["bind_source"] == "final_answer_fallback"


def test_score_uses_tool_input_for_object_key():
    from services.scenario_eval_service import _score_step
    from services.scenario_step_trace_service import (
        _resolve_step_score_text,
        extract_observation_io,
    )

    obs = {
        "id": "o_in",
        "name": "tool:ontology_context",
        "type": "TOOL",
        "input": {"object_key": "海况", "ontology_id": "maritime-defense-agent"},
        "output": {"status": "completed", "output_preview": "# 实体属性清单"},
        "metadata": {"iteration": 3},
    }
    io = extract_observation_io(obs)
    text, src = _resolve_step_score_text(
        "S1", io, {"bind_source": "heuristic"}, agent_answer=""
    )
    assert "海况" in text
    score, passed, _ = _score_step(
        text,
        {
            "keywords": ["0.727", "225"],
            "alt_keywords": ["海况", "object_key", "conditionId"],
        },
    )
    assert score > 0
    assert passed
    assert src == "span_output"


def test_bind_s1_ontology_context_iteration3():
    """海况研判常在 tool:ontology_context（较晚 iteration），应能绑定并计分。"""
    ontology_out = {
        "object_key": "海况",
        "output_preview": "# 实体属性清单 conditionId windSpeed waveHeight 海况",
    }
    observations = [
        _obs("o1", "llm:main_iteration", "开始分析…", 1),
        _obs(
            "o2",
            "tool:ontology_context",
            '{"object_key":"海况","markdown":"# 实体属性清单\\nconditionId\\nwindSpeed\\nwaveHeight"}',
            3,
        ),
    ]
    obs, meta = bind_step_observation(
        "S1",
        observations,
        binding={
            "span_name_patterns": ["tool:ontology_context", "llm:main_iteration"],
            "iteration_lte": 6,
            "iteration_lte_llm_only": True,
            "output_keywords": ["海况", "object_key", "conditionId", "windSpeed", "waveHeight"],
            "label": "态势综合研判",
        },
    )
    assert obs is not None
    assert obs["id"] == "o2"
    assert meta["bind_source"] == "heuristic"

    from services.scenario_eval_service import _score_step
    from services.scenario_step_trace_service import (
        _resolve_step_score_text,
        extract_observation_io,
    )

    io = extract_observation_io(obs)
    text, _ = _resolve_step_score_text(
        "S1", io, meta, agent_answer=""
    )
    score, passed, ev = _score_step(
        text,
        {
            "keywords": ["0.727", "225", "B001", "B008"],
            "alt_keywords": [
                "海况",
                "conditionId",
                "windSpeed",
                "waveHeight",
                "实体属性",
                "object_key",
            ],
        },
    )
    assert score > 0
    assert passed
    assert ev.get("score_group") == "alt"


def test_bind_s1_no_final_answer_fallback():
    """S1 非汇总步骤：无关键词命中 + 有终局答案 → final_answer_no_span。

    onto-agent 并行处理：步骤 evidence 可能跨多个 span 或仅在终局答案中，
    span output 不含步骤关键词 → score=0 → 用终局答案评测（return None），
    但保留 best span 的 observation_id 供调试。
    """
    observations = [_obs("o1", "tool:ontology_context", "partial", 1)]
    final = "海况系数 0.727 威胁方向 225°"
    obs, meta = bind_step_observation("S1", observations, final_answer_text=final)
    assert obs is None, "score=0 + 有终局答案 → return None"
    assert meta["bind_source"] == "final_answer_no_span"
    assert meta.get("observation_id") == "o1", "应保留 best span 的 observation_id"


def test_keyword_match_output_only():
    """关键词匹配只看 output，tool span input 含关键词但 output 不含 → score=0。

    根因修复：_observation_text_blob 拼接 input+output+metadata 导致 tool span
    的 input（查询参数/本体数据/用户 query）造成误命中。改为 _observation_output_blob
    仅匹配 output。

    场景：tool:ontology_context span 的 input 含业务编号（AMV/UAV）等查询参数，
    output 仅为工具调用结果摘要（"查询完成"），不含步骤关键词。
    步骤关键词 "AMV" 出现在 input 而非 output → 应 score=0 → final_answer_no_span。
    """
    observations = [
        {
            "id": "tool_in",
            "name": "tool:ontology_context",
            "type": "TOOL",
            "input": {
                "object_key": "AMV",
                "ontology_id": "maritime-defense-agent",
                "query": "AMV 可用总数 授权上限",
            },
            "output": {"status": "completed", "output_preview": "查询完成"},
            "metadata": {"iteration": 2},
        },
    ]
    final = "AMV 可用总数 3 授权上限 3 两栖突击营"
    obs, meta = bind_step_observation(
        "S2",
        observations,
        binding={
            "span_name_patterns": ["tool:ontology_context", "llm:main_iteration"],
            "output_keywords": ["AMV", "授权上限", "两栖突击营"],
            "label": "兵力授权校验",
        },
        final_answer_text=final,
    )
    assert obs is None, "score=0 + 有终局答案 → return None"
    assert meta["bind_source"] == "final_answer_no_span"
    assert meta.get("observation_id") == "tool_in", "应保留 best span 的 observation_id"


def test_score_s8_zero_when_span_output_missing_keywords():
    from services.scenario_eval_service import _score_step
    from services.scenario_step_trace_service import (
        _resolve_step_score_text,
        extract_observation_io,
    )

    io = extract_observation_io(_obs("o1", "llm:main_iteration", "部分方案无快艇指令", 3))
    text, src = _resolve_step_score_text(
        "S8",
        io,
        {"bind_source": "heuristic_no_keyword"},
        agent_answer="方案已完成但未列出 CMD",
    )
    cmd_ids = [f"CMD_{i:03d}" for i in range(1, 9)]

    score, passed, ev = _score_step(
        text,
        {"required_cmd_ids": cmd_ids, "keywords": ["航路点"]},
    )
    assert score == 0.0
    assert not passed
    assert ev.get("matched_cmd_ids") == []
    assert len(ev.get("missing_cmd_ids") or []) == 8
    assert src == "span_output"


def test_extract_io_and_langfuse_url():
    obs = _obs("abc123", "llm:main_iteration", "answer text", 2)
    io = extract_observation_io(obs)
    assert "answer text" in io["output_preview"]
    urls = build_langfuse_trace_urls(
        trace_id="trace-xyz",
        observation_id="abc123",
        langfuse_project=None,
    )
    assert urls["trace_url"] is not None
    assert "abc123" in (urls["observation_url"] or "")


def test_apply_scenario_enforce_order_monotonic():
    from services.scenario_step_trace_service import _apply_scenario_enforce_order

    observations = [
        _obs("o1", "llm:main", "step1", 1),
        _obs("o2", "llm:main", "step2", 2),
    ]
    step_results = [
        {
            "step_key": "S1",
            "step_order": 1,
            "enforce_order": True,
            "passed": True,
            "evidence": {},
            "trace_binding": {"observation_id": "o2"},
        },
        {
            "step_key": "S2",
            "step_order": 2,
            "enforce_order": True,
            "passed": True,
            "evidence": {},
            "trace_binding": {"observation_id": "o1"},
        },
    ]
    _apply_scenario_enforce_order(observations, step_results)
    assert step_results[0]["evidence"]["order_ok"] is True
    assert step_results[1]["evidence"]["order_ok"] is False
    assert step_results[1]["passed"] is False


def test_bind_tiebreaker_cross_step_purity():
    """score 平局时，-cross_step_hits tiebreaker 选跨步骤命中更少的 span。

    复现真实 trace b59b1fd5 Q2-S2 错绑：iter20（终局合并答案）与 iter17（纯 S2）
    对 S2 的 own score 相同，但 iter20 同时命中 S3 关键词（cross_step_hits=1），
    iter17 不命中 S3（cross_step_hits=0）。tiebreaker 应选 iter17。
    """
    s2_binding = {
        "span_name_patterns": ["llm:main_iteration", "tool:"],
        "output_keywords": ["T-001", "自行火炮", "识别置信度", "主锁节点"],
        "label": "主目标杀伤链构建",
    }
    peer_keywords = {
        "S1": ["小簇头", "网络汇聚"],
        "S3": ["察打评", "P1开进展开", "条件兜底攻击"],
    }
    # iter17：纯 S2 内容，own=4，不命中任何 peer → cross_step_hits=0
    iter17 = _obs(
        "o_pure_s2",
        "llm:main_iteration",
        "主目标 T-001 自行火炮，识别置信度 0.91，主锁节点 UAV-03",
        iteration=17,
    )
    # iter20：终局合并答案，own=4（同样命中 S2 全部 kw），但顺带命中 S3 → cross_step_hits=1
    iter20 = _obs(
        "o_merged_final",
        "llm:main_iteration",
        "主目标 T-001 自行火炮，识别置信度 0.91，主锁节点 UAV-03。"
        "条件兜底攻击由突击组执行，P1开进展开阶段。",
        iteration=20,
    )
    observations = [iter17, iter20]

    obs, meta = bind_step_observation(
        "S2",
        observations,
        binding=s2_binding,
        peer_step_keywords=peer_keywords,
    )
    assert obs is not None
    assert obs["id"] == "o_pure_s2", "score 平局时应选 cross_step_hits 更小的纯 S2 span"
    assert meta["cross_step_hits"] == 0
    assert meta["bind_source"] == "heuristic"


def test_bind_tiebreaker_no_peer_keywords_unchanged():
    """未传 peer_step_keywords 时，cross_step_hits 恒为 0，排序退化为原逻辑（向后兼容）。"""
    s2_binding = {
        "span_name_patterns": ["llm:main_iteration"],
        "output_keywords": ["T-001", "自行火炮"],
        "label": "主目标杀伤链构建",
    }
    # iter20 output 更长，原 tiebreaker（output_len）会选它
    iter17 = _obs("o_short", "llm:main_iteration", "T-001 自行火炮", iteration=17)
    iter20 = _obs("o_long", "llm:main_iteration", "T-001 自行火炮，详细分析", iteration=20)
    observations = [iter17, iter20]

    obs, meta = bind_step_observation("S2", observations, binding=s2_binding)
    assert obs is not None
    # 未传 peer_step_keywords → cross_step_hits=0，不影响排序，按 output_len 选 o_long
    assert obs["id"] == "o_long"
    assert meta["cross_step_hits"] == 0
