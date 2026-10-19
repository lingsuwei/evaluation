"""从 golden model_answer（及可选 ground_truth）提炼场景子步骤草稿。"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from services.scenario_gt_draft_service import (
    _DATA_FLAG_RE,
    _extract_keywords_from_block,
    _normalize_for_dedup,
    draft_steps_from_markdown,
    is_noise_section,
    is_specific_value_keyword,
    is_structural_noise_keyword,
)

# 行首步骤标题（按优先级尝试）
_STEP_LINE_PATTERNS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"^步骤\s*(\d+(?:-\d+)*)\s*[:：]?\s*(.*)$"), "S"),
    (re.compile(r"^第\s*([一二三四五六七八九十百\d]+)\s*步\s*[:：]?\s*(.*)$"), "S"),
    (re.compile(r"^SP(\d+)\s*[:：]?\s*(.*)$", re.I), "SP"),
    (re.compile(r"^子问题\s*(\d+)\s*[:：]?\s*(.*)$"), "Q"),
    # N 模式：数字 + 分隔符(、／．) + 标题。
    # 用 (?!\d) 否定先行断言排除 "1.1"、"1.2" 等小数编号子步骤，
    # 只识别一级编号（如 "1、态势综合研判" / "1. 态势综合研判"）。
    # 注意：当文本中同时存在 S/SP 级标记（第一步/步骤1/第1步/SP1）时，
    # N 模式命中会在 _split_sections_by_patterns 中被丢弃（视为子步骤）。
    (re.compile(r"^(\d+)(?:[、．]|\.(?!\d))\s*(.+)$"), "N"),
]

# 行首装饰前缀：emoji 与常见装饰符号（📋 ✅ 🔹 • ● ◆ ▶ ➤ ✔ ✳ ❌ 等）
# 注意：不要用 [\u2B00-\U0001F2FF] 这类宽区间，它会误伤 CJK 汉字（如"步骤"的"步"\u6b65 / "骤"\u9aa4）。
_LEADING_DECORATION_RE = re.compile(
    r"^(?:"
    r"[\U0001F300-\U0001FAFF]"           # emoji symbols & pictographs
    r"|[\u2600-\u27BF]"                  # misc symbols & dingbats
    r"|[\u2190-\u21FF]"                  # arrows
    r"|[✓✔✳✴❇❌❎❗❕❘❙❚❛❜❝❞★☆◆◇○●□■▶▷◀▷➤➜➞➢➣]"
    r"|[*•·▪◦‣⁃]"
    r")\s*"
)


def _strip_leading_decoration(s: str) -> str:
    """循环剥除行首的 emoji/装饰符号前缀，处理 "📋 ✅ 步骤1" 这类叠加装饰。"""
    prev = None
    while prev != s:
        prev = s
        s = _LEADING_DECORATION_RE.sub("", s, count=1)
    return s


_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_num_to_int(raw: str) -> int:
    raw = (raw or "").strip()
    if raw.isdigit():
        return int(raw)
    if len(raw) == 1 and raw in _CN_NUM:
        return _CN_NUM[raw]
    if raw == "十":
        return 10
    if raw.startswith("十") and len(raw) == 2 and raw[1] in _CN_NUM:
        return 10 + _CN_NUM[raw[1]]
    return 0


def _normalize_step_title(prefix: str, num_raw: str, rest: str) -> Tuple[str, str]:
    """返回 (step_key, display_name)。"""
    if prefix == "S":
        if num_raw.isdigit():
            n = int(num_raw)
            key = f"S{n}"
        else:
            cn_val = _cn_num_to_int(num_raw)
            if cn_val > 0:
                n = cn_val
                key = f"S{n}"
            else:
                # 复合编号如 "3-1"/"3-2"，保留原样避免去重碰撞
                n = num_raw
                key = f"S{num_raw}"
    elif prefix == "SP":
        key = f"SP{num_raw}"
        n = int(num_raw)
    elif prefix == "Q":
        key = f"Q{num_raw}"
        n = int(num_raw)
    else:
        key = f"N{num_raw}"
        n = int(num_raw)
    rest_stripped = (rest or "").strip()
    if prefix == "S" and rest_stripped:
        title = f"步骤{n}： {rest_stripped}"
    else:
        title = rest_stripped or f"步骤{n}"
    if len(title) > 80:
        title = title[:77] + "…"
    return key, title


def _split_sections_by_patterns(text: str) -> List[Dict[str, Any]]:
    lines = text.splitlines()
    hits: List[Tuple[int, str, str, str]] = []
    for line_no, line in enumerate(lines):
        stripped = _strip_leading_decoration(line.strip())
        if not stripped or len(stripped) < 2:
            continue
        for pat, prefix in _STEP_LINE_PATTERNS:
            m = pat.match(stripped)
            if not m:
                continue
            num_raw = m.group(1)
            rest = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
            # 过滤纯数字列表误匹配（如版本号行）
            if prefix == "N" and rest and len(rest) > 120:
                continue
            key, title = _normalize_step_title(prefix, num_raw, rest)
            hits.append((line_no, key, title, prefix))
            break

    if len(hits) < 2:
        return []

    # 优先级排除：当存在 S/SP 级步骤标记（第一步/步骤1/第1步/SP1）时，
    # N 模式（1、2、3）视为子步骤，不作为独立步骤识别。
    has_high_level = any(h[3] in ("S", "SP") for h in hits)
    if has_high_level:
        hits = [h for h in hits if h[3] != "N"]
        if len(hits) < 2:
            return []

    # 同一 key 去重，保留首次
    seen_keys: set[str] = set()
    unique_hits: List[Tuple[int, str, str, str]] = []
    for h in hits:
        if h[1] in seen_keys:
            continue
        seen_keys.add(h[1])
        unique_hits.append(h)

    sections: List[Dict[str, Any]] = []
    for i, (start_line, key, title, _pfx) in enumerate(unique_hits):
        end_line = unique_hits[i + 1][0] if i + 1 < len(unique_hits) else len(lines)
        body = lines[start_line + 1 : end_line]
        sections.append({"step_key": key, "title": title, "body": body})
    return sections[:12]


def _clean_criterion_line(line: str) -> Optional[str]:
    """清理单行 criterion：去列表前缀、转 tab 为冒号、过滤表头行。返回 None 表示该行应跳过。"""
    c = line.strip()
    if not c:
        return None
    # 去列表前缀
    c = re.sub(r"^[-*•·▪◦‣⁃\d]+[.、．:：\)]?\s*", "", c)
    if not c:
        return None
    # 过滤表头行：含 tab 且是多列纯文字（无数据标志）
    if "\t" in c:
        parts = [p.strip() for p in c.split("\t") if p.strip()]
        if len(parts) >= 3:
            # 表头特征：≥3列，所有列都很短（≤12字），且不含数据标志（数字/✓✅/特定符号）
            has_data = any(re.search(r"[0-9✓✅@]", p) for p in parts)
            all_short = all(len(p) <= 12 for p in parts)
            if all_short and not has_data:
                return None  # 纯表头行，跳过
        elif len(parts) == 2:
            # 2列表头：如"字段\t值"，归一化后总长≤3且无数据标志
            joined_norm = re.sub(r"\s+", "", "".join(parts))
            if len(joined_norm) <= 3 and not any(
                re.search(r"[0-9✓✅@]", p) for p in parts
            ):
                return None
            c = f"{parts[0]}：{parts[1]}"
        else:
            c = " ".join(parts)
    return c


def _truncate_body(body: str, max_len: int = 2000) -> str:
    """截断 body 到指定长度，优先保留含数据标志的行。"""
    if not body or len(body) <= max_len:
        return body or ""
    lines = body.splitlines()
    # 优先保留含数据标志的行（数字/✓✅/@/特定编号格式）
    data_lines = [ln for ln in lines if re.search(r"[0-9✓✅@]|INTEL|ENV|DT-|R0\d|UAV|LM-|AMV|DOG|WRV|UGV|T-0", ln)]
    other_lines = [ln for ln in lines if ln not in data_lines]
    result = ""
    for ln in data_lines + other_lines:
        if len(result) + len(ln) + 1 > max_len:
            break
        result = f"{result}\n{ln}" if result else ln
    return result or body[:max_len]


# 推理类词汇
_REASONING_WORDS: Tuple[str, ...] = (
    "判定", "判断", "因为", "所以", "分析", "推导", "计算", "选择", "决定",
    "得出", "结论", "由此", "据此", "综上", "因此", "认为", "发现", "确认",
    "验证", "证明", "排除", "归纳", "推理", "评估", "衡量",
)

# 具体值型关键词过滤委托给 scenario_gt_draft_service.is_specific_value_keyword


def _detect_eval_mode(
    full_body: str,
    criteria: List[str],
    keywords: List[str],
) -> str:
    """根据步骤内容特征自动判断评测模式：keyword 或 llm_aggregate。

    规则（优先级从高到低）：
    1. 含空数组/空对象（[]、{}）               → LLM（[]="无"需语义理解）
    2. JSON 密度 > 10%                        → LLM（结构化 JSON 输出）
    3. 推理类词汇密度 > 5%                    → LLM（需语义理解推理过程）
    4. 平均行长度 > 30 字符且表格行占比 > 40%    → LLM（长句描述，关键词验证不了语义）
    5. 坐标/数值行密度 > 15%                   → LLM（精确数值匹配过于严格，需语义对比）
    6. body 去空白后 > 1500 字符               → LLM（内容过长，关键词难以全面覆盖）
    7. 关键词含坐标/长编号格式                  → LLM（精确匹配不适用于结构化数据）
    8. 默认                                    → keyword（短文本、简单声明性内容适合关键词匹配）
    """
    if not full_body:
        return "llm_aggregate"

    norm = full_body.replace(" ", "").replace("\n", "")
    total_chars = len(norm)
    if total_chars == 0:
        return "llm_aggregate"

    # 规则1：含空数组/空对象
    if "[]" in norm or "{}" in norm:
        return "llm_aggregate"

    # 规则2：JSON 密度 > 10%
    json_chars = sum(norm.count(c) for c in "{}[]\":")
    if json_chars / total_chars > 0.10:
        return "llm_aggregate"

    # 规则3：推理类词汇密度 > 5%
    reasoning_hits = sum(norm.count(w) for w in _REASONING_WORDS)
    if reasoning_hits > 0:
        # 按字符数算密度（每个推理词约2字），>5% 即推理词总字数占比超 5%
        reasoning_chars = sum(len(w) * norm.count(w) for w in _REASONING_WORDS)
        if reasoning_chars / total_chars > 0.05:
            return "llm_aggregate"

    # 规则4：平均行长度 > 30 字符 且表格行占比 > 40%
    body_lines = [ln for ln in full_body.splitlines() if ln.strip()]
    if body_lines:
        avg_line_len = sum(len(ln.strip()) for ln in body_lines) / len(body_lines)
        def _is_table_row(ln: str) -> bool:
            s = ln.strip()
            if "\t" in s or s.count("|") >= 2:
                return True
            parts = re.split(r"\s+", s)
            # ≥4 段空格分隔且前3段都是短字段（≤20字符）→ 表格行
            if len(parts) >= 4 and all(len(p) <= 20 for p in parts[:3]):
                return True
            return False
        table_lines = sum(1 for ln in body_lines if _is_table_row(ln))
        table_ratio = table_lines / len(body_lines)
        if avg_line_len > 30 and table_ratio > 0.40:
            return "llm_aggregate"

    # 规则5：坐标/数值行密度 > 15% → LLM
    # 匹配坐标对 [数字, 数字]、航路点等精确数值内容
    _COORD_RE = re.compile(r"\[\d+\.?\d*[,\s]+\d+\.?\d*\]")
    coord_lines = sum(1 for ln in body_lines if _COORD_RE.search(ln))
    if body_lines and coord_lines / len(body_lines) > 0.15:
        return "llm_aggregate"

    # 规则6：body 去空白后 > 1500 字符 → LLM（关键词难以全面覆盖长内容）
    if total_chars > 1500:
        return "llm_aggregate"

    # 规则7：关键词含坐标/长编号格式 → LLM（精确匹配不适用于结构化标识符）
    if keywords:
        # 坐标类：逗号分隔的两个小数（如 144.929,13.317）或方括号包围
        _coord_kw_re = re.compile(r"\d+\.\d+,\s*\d+\.\d+|\[\d+\.?\d*[,\s]+\d+\.?\d*\]")
        has_structured_kw = any(
            _coord_kw_re.search(kw)
            # 长编号类：含 4 位以上连续数字且总长 > 12
            or (re.search(r"\d{4,}", kw) and len(kw) > 12)
            for kw in keywords
        )
        if has_structured_kw:
            return "llm_aggregate"

    # 规则8：默认 → keyword（短文本、简单声明性内容适合关键词匹配）
    return "keyword"


def _auto_generate_aggregate_prompt(
    step_name: str,
    full_body: str,
    criteria: List[str],
    keywords: List[str],
) -> str:
    """根据步骤的完整 body 自动生成 LLM 评测提示词（参考答案对比模式）。"""
    name_display = step_name or "本步骤"
    truncated_body = _truncate_body(full_body, max_len=2000)

    # 固化评分维度引导（仅自动生成时注入，用户自定义 prompt 不受影响）
    dimensions_section = """
【评分维度引导】（请依次从以下四个维度思考，综合给出 score）
- 覆盖率（权重 0.4）：Agent 输出覆盖参考答案/评测点中关键信息的程度
- 准确性（权重 0.3）：数值、实体、事实声明是否准确无误
- 完整性（权重 0.2）：是否遗漏必要内容（如必含字段、关键结论）
- 清晰度（权重 0.1）：输出格式是否符合要求，表述是否清晰可读"""

    # 有完整 body 时：用参考答案对比模式
    if truncated_body:
        return f"""【评测任务说明】
本步骤为"{name_display}"，请对比 Agent 输出与参考答案的语义一致性。
Agent 输出可能是 JSON、表格或纯文本，请按语义判断，不要因格式差异扣分。
Agent 输出可能包含工具调用日志、中间推理等非答案内容，请仅关注与评测标准相关的实质性输出，忽略非答案噪音。

【JSON 语义约定】
- 空数组 [] 等价于"无"或"空"
- 数组 ["X"] 等价于"包含 X"
- 字段名与其值在 JSON 中会被语法符号（引号、冒号、方括号）分隔，应合并理解，不应因此判未命中

【参考答案（该步骤的标准 output）】
{truncated_body}
{dimensions_section}

【评分说明】
- 根据覆盖率打分：Agent 输出覆盖参考答案中评测点的程度
- reason 中请说明各评测点的满足情况及扣分原因"""

    # 无 body 时：回退到评测点模式（只描述评测点，评分尺度交给 _build_judge_prompt 统一处理）
    points: List[str] = []
    seen_norms: set = set()
    for c in criteria[:10]:
        cleaned = _clean_criterion_line(c)
        if not cleaned:
            continue
        norm = _normalize_for_dedup(cleaned)
        if norm in seen_norms:
            continue
        seen_norms.add(norm)
        points.append(cleaned)
    if len(points) < 3 and keywords:
        name_norm = _normalize_for_dedup(step_name)
        for kw in keywords[:6]:
            if not kw:
                continue
            kw_norm = _normalize_for_dedup(kw)
            if kw_norm in seen_norms or kw_norm == name_norm:
                continue
            seen_norms.add(kw_norm)
            points.append(f"输出中应包含：{kw}")
    if not points:
        points.append("输出内容与步骤要求语义相关，包含关键信息")

    points_text = "\n".join(f"{i}. {p}" for i, p in enumerate(points, start=1))
    return f"""【评测任务说明】
本步骤为"{name_display}"，要求 Agent 完成上述任务并输出结果（可能是 JSON、表格或纯文本）。

【JSON 语义约定】
- 空数组 [] 等价于"无"或"空"
- 字段名与其值在 JSON 中会被语法符号分隔，应合并理解

【评测点】
请逐项检查以下评测点，根据满足程度评分：
{points_text}
{dimensions_section}

【评分说明】
- 根据满足评测点的覆盖率评分
- reason 中请说明各评测点的满足情况及扣分原因"""


def _ensure_llm_aggregate_config(step: Dict[str, Any]) -> None:
    """给已有步骤补充评测配置（用于 Markdown fallback 路径）。

    根据 _detect_eval_mode 自动判断 keyword 或 llm_aggregate。
    """
    mr = step.get("match_rules")
    if not isinstance(mr, dict):
        mr = {}
        step["match_rules"] = mr
    ec = mr.get("eval_config")
    if not isinstance(ec, dict) or not ec.get("mode"):
        criteria = step.get("gt_criteria") or []
        if isinstance(criteria, str):
            criteria = [criteria]
        gt_exp = step.get("gt_expected") or {}
        if not isinstance(gt_exp, dict):
            gt_exp = {}
        kws = gt_exp.get("keywords") or []
        # 优先用 gt_expected.text 作为参考答案；没有则从 criteria 重建
        full_body = gt_exp.get("text") or ("\n".join(criteria) if criteria else "")
        eval_mode = _detect_eval_mode(full_body, list(criteria), list(kws))
        eval_config: Dict[str, Any] = {"mode": eval_mode}
        if eval_mode == "llm_aggregate":
            prompt = _auto_generate_aggregate_prompt(
                step.get("name") or step.get("step_key") or "",
                full_body,
                list(criteria),
                list(kws),
            )
            eval_config["aggregate_prompt"] = prompt
        mr["eval_config"] = eval_config
        # 确保 gt_expected.text 存在，供Ground_Truth评测终局补分使用
        if not gt_exp.get("text") and full_body:
            gt_exp["text"] = full_body
            step["gt_expected"] = gt_exp


def _sections_to_steps(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not sections:
        return []
    # 过滤噪声段（过渡性/无实质可评测内容）
    filtered = [sec for sec in sections if not is_noise_section(sec)]
    if not filtered:
        # 全被过滤 → 返回空，让上层走单步兜底
        return []
    sections = filtered
    n = len(sections)
    weight = round(1.0 / n, 4)
    steps: List[Dict[str, Any]] = []
    for idx, sec in enumerate(sections, start=1):
        body_lines = [ln for ln in sec["body"] if ln.strip()]
        criteria: List[str] = []
        for ln in body_lines[:80]:
            t = ln.strip()
            if t:
                criteria.append(t[:200])
        title_line = [sec["title"]] if sec.get("title") else []
        # 过滤表头行后再提取关键词，避免表头碎片进入 keywords（与判分细则同源，覆盖80行）
        kw_source = title_line + [ln for ln in body_lines[:80] if _clean_criterion_line(ln) is not None]
        keywords = _extract_keywords_from_block(kw_source)
        # 过滤具体值型关键词（golden answer 的具体数值/公式结果），
        # 这类 keyword 不适合子串精确匹配，保留会导致系统性 0 命中
        # 同时过滤结构性噪声（步骤编号/过渡话术/纯通用过程词），领域无关
        keywords = [
            kw for kw in keywords
            if not is_specific_value_keyword(kw) and not is_structural_noise_keyword(kw)
        ]
        if not keywords and sec["title"]:
            compact = re.sub(r"\s+", "", sec["title"])
            if 2 <= len(compact) <= 24:
                keywords = [compact[:24]]
        step_key = sec.get("step_key") or f"S{idx}"
        full_body = "\n".join(body_lines)
        eval_mode = _detect_eval_mode(full_body, criteria, keywords)
        agg_prompt = ""
        if eval_mode == "llm_aggregate":
            agg_prompt = _auto_generate_aggregate_prompt(
                sec["title"] or "", full_body, criteria, keywords
            )
        steps.append(
            {
                "step_key": step_key,
                "name": sec["title"],
                "step_order": idx,
                "weight": weight,
                "required": True,
                "enabled": True,
                "gt_criteria": criteria or [sec["title"]],
                "gt_expected": {"keywords": keywords[:12], "text": full_body},
                "match_rules": {
                    "eval_config": {
                        "mode": eval_mode,
                        **({"aggregate_prompt": agg_prompt} if agg_prompt else {}),
                    },
                    "trace_binding": {
                        "span_name_patterns": ["llm:main_iteration", "tool:"],
                        "output_keywords": keywords[:6],
                    },
                },
            }
        )
    return steps


def draft_steps_from_model_answer(
    model_answer: str,
    *,
    question: Optional[str] = None,
    ground_truth: Optional[str] = None,
    scenario_key: Optional[str] = None,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    从 model_answer 提炼子步骤；若无结构则回退 ground_truth Markdown，再回退单步。
    """
    warnings: List[str] = []
    primary = (model_answer or "").strip()
    fallback_gt = (ground_truth or "").strip()
    q_hint = (question or "").strip()[:40]

    sections = _split_sections_by_patterns(primary) if primary else []
    steps = _sections_to_steps(sections)

    if not steps and fallback_gt:
        md_draft = draft_steps_from_markdown(
            fallback_gt, scenario_key=scenario_key, name=name
        )
        steps = md_draft.get("steps") or []
        if steps:
            warnings.append("model_answer 无步骤结构，已从 ground_truth 提炼")
            for s in steps:
                s["enabled"] = True
                _ensure_llm_aggregate_config(s)

    if not steps and primary:
        md_draft = draft_steps_from_markdown(primary, scenario_key=scenario_key, name=name)
        steps = md_draft.get("steps") or []
        if steps:
            warnings.append("已按 Markdown 标题切分 model_answer")
            for s in steps:
                _ensure_llm_aggregate_config(s)

    if not steps:
        blob = primary or fallback_gt or q_hint
        kws = _extract_keywords_from_block(blob.splitlines()[:80]) if blob else []
        fallback_criteria = ["输出包含关键结论"] if q_hint else []
        s1_name = q_hint or "整体推理"
        s1_eval_mode = _detect_eval_mode(blob, fallback_criteria, kws[:8])
        s1_prompt = ""
        if s1_eval_mode == "llm_aggregate":
            s1_prompt = _auto_generate_aggregate_prompt(s1_name, blob, fallback_criteria, kws[:8])
        steps = [
            {
                "step_key": "S1",
                "name": s1_name,
                "step_order": 1,
                "weight": 1.0,
                "required": True,
                "enabled": True,
                "gt_criteria": fallback_criteria,
                "gt_expected": {"keywords": kws[:8], "text": blob},
                "match_rules": {
                    "eval_config": {
                        "mode": s1_eval_mode,
                        **({"aggregate_prompt": s1_prompt} if s1_prompt else {}),
                    },
                    "trace_binding": {
                        "span_name_patterns": ["llm:main_iteration", "tool:"],
                        "output_keywords": kws[:6],
                    }
                },
            }
        ]
        warnings.append("未识别多步结构，已生成单步兜底")

    display_name = name or (f"Q {q_hint}" if q_hint else "按题场景子步骤")
    return {
        "scenario_key": scenario_key,
        "name": display_name,
        "description": "由 model_answer 自动提炼，请人工校对",
        "steps": steps,
        "warnings": warnings,
    }


# ── LLM 跨步骤辨识关键词提取（通用，领域无关）──
# 正则提取的关键词常含装备通名/步骤标题等无辨识力词条，
# 在 bind_step_observation 中 0 命中后排序键偏向早期迭代，
# 导致步骤绑错 span。LLM 看到全部步骤可做跨步骤去重，选出
# 仅在本步骤正文出现、且 agent 会原样复述的短语。

_KEYWORD_EXTRACTION_PROMPT = """你是关键词提取器。下面是一道题的若干场景子步骤。为每个步骤提取 3-6 个"步骤辨识关键词"。

要求：
1. 关键词必须直接来自该步骤正文（原文子串，不改写）
2. 关键词在其他步骤的正文中很少出现——能将此步骤与其他步骤区分开
3. 优先选：实体名、专有术语、具体技术短语、特色动宾搭配（agent 输出时会原样复述的词）
   - 最优先：实体编号（如 INTEL-26-08-01、ENV-26-08、T-001、OPORD-26-08）和装备型号（AMV、UAV、LM、DOG），这些在 agent 工具调用的 input.key/entity/query 中原样出现，跨步骤辨识力最强
   - 次优先：专有术语（威胁等级、环境态势、自行火炮、察打评）
4. 不要选：步骤编号/标题（如"步骤1"）、过渡话术（"进入下一步"）、通用过程词（步骤/输出/分析/结果/判断/校验）、纯数字、坐标对、公式、标点
5. 每个关键词长度 2-20 字

{steps_text}

输出 JSON 数组，不要解释：
[{"step_key":"S1","keywords":["关键词1","关键词2"]},{"step_key":"S2","keywords":["关键词1","关键词2"]}]"""


def _parse_keyword_json(resp: str) -> Dict[str, List[str]]:
    """容错解析 LLM 返回的关键词 JSON。

    返回 {step_key: [keyword,...]} 映射；解析失败返回空 dict。
    """
    if not resp or not resp.strip():
        return {}
    text = resp.strip()
    # 提取首个 [...] 块（LLM 可能在 JSON 前后加解释文字）
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return {}
    chunk = text[start : end + 1]
    try:
        arr = json.loads(chunk)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(arr, list):
        return {}
    mapping: Dict[str, List[str]] = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        sk = item.get("step_key")
        kws = item.get("keywords")
        if not isinstance(sk, str) or not isinstance(kws, list):
            continue
        mapping[sk] = [str(k).strip() for k in kws if isinstance(k, str) and k.strip()]
    return mapping


# 步骤标题前缀：步骤N：/ 第N步：/ SP N：/ 子问题N：
_TITLE_PREFIX_RE = re.compile(
    r"^(?:步骤\s*\d+\s*[:：]?|第[一二三四五六七八九十百\d]+步\s*[:：]?|SP\s*\d+\s*[:：]?|子问题\s*\d+\s*[:：]?)\s*(.*)$"
)
# 标题拆分分隔符
_TITLE_SPLIT_RE = re.compile(r"[、/和以及，,]")


def _extract_fallback_keywords(title: str, body_text: str) -> List[str]:
    """LLM 提取失败/无辨识力时的兜底关键词：从步骤名拆词 + 正文实体编号。

    1. 从标题拆词：去除"步骤N："前缀，按、/和/以及拆分，过滤通用词与结构性噪声
    2. 从正文补实体编号：用 _DATA_FLAG_RE 匹配 INTEL/ENV/T-0\\d/OPORD/装备型号等
       （实体编号在 agent 工具 input.key/entity/query 中会原样出现，辨识力强）

    返回去重保序的关键词列表（上限 8 个）。
    """
    kws: List[str] = []

    # 1. 标题拆词
    if title:
        m = _TITLE_PREFIX_RE.match(title)
        clean = m.group(1).strip() if m else title.strip()
        if clean:
            for part in _TITLE_SPLIT_RE.split(clean):
                part = part.strip()
                if 2 <= len(part) <= 12 and not is_structural_noise_keyword(part):
                    kws.append(part)

    # 2. 正文实体编号
    if body_text:
        for m in _DATA_FLAG_RE.finditer(body_text):
            val = m.group(0).strip()
            if (
                val
                and 2 <= len(val) <= 20
                and not is_specific_value_keyword(val)
                and not is_structural_noise_keyword(val)
            ):
                kws.append(val)

    # 去重保序
    seen: set[str] = set()
    out: List[str] = []
    for kw in kws:
        n = _normalize_for_dedup(kw)
        if n and n not in seen:
            seen.add(n)
            out.append(kw)
    return out[:8]


async def enhance_step_keywords_with_llm(
    steps: List[Dict[str, Any]],
    *,
    model_key: str = "auto",
) -> List[Dict[str, Any]]:
    """LLM 跨步骤辨识关键词提取（通用，领域无关）。

    LLM 同时看到所有步骤的标题+正文，为每个步骤挑选：
    - 来自该步骤正文的原文子串（去空格后仍能在正文中匹配）
    - 在其他步骤正文中很少出现（跨步骤辨识力）
    - agent 输出中会原样复述的短语（实体名/专有术语/特色短语）

    失败时静默回退到正则关键词（保持原值），不阻断提炼流程。
    """
    if not steps or len(steps) < 2:
        return steps  # 单步无需跨步骤辨识

    # 构造 LLM 输入：每个步骤的标题 + 正文（截断防 token 爆炸）
    blocks: List[str] = []
    for s in steps:
        title = (s.get("name") or "").strip()
        gt_exp = s.get("gt_expected") or {}
        body = (gt_exp.get("text") or "").strip()
        if not body:
            criteria = s.get("gt_criteria") or []
            body = "\n".join(criteria) if isinstance(criteria, list) else str(criteria)
        body = body[:800]
        blocks.append(f"【{s.get('step_key')}】标题：{title}\n正文：\n{body}")
    prompt = _KEYWORD_EXTRACTION_PROMPT.replace("{steps_text}", "\n\n".join(blocks))

    try:
        from evaluators.llm_client import evaluate_with_llm

        resp = await evaluate_with_llm(
            prompt,
            system_prompt="你是关键词提取器，只输出 JSON。",
            model_key=model_key,
            override_temperature=0.0,
            override_max_tokens=2048,
        )
        mapping = _parse_keyword_json(resp)
    except Exception:
        return steps  # LLM 失败/超时/非法响应，回退正则关键词

    if not mapping:
        return steps

    # 回填：校验每个 keyword 必须是 step 正文（去空格去标点后）的子串
    for s in steps:
        sk = s.get("step_key")
        kws = mapping.get(sk) or []
        gt_exp = s.get("gt_expected") or {}
        body_text = gt_exp.get("text") or ""
        body_norm = _normalize_for_dedup(body_text)
        if not body_norm:
            continue
        valid: List[str] = []
        seen_norm: set[str] = set()
        for kw in kws:
            kw_clean = kw.strip()
            if not kw_clean or len(kw_clean) > 20:
                continue
            if is_structural_noise_keyword(kw_clean):
                continue
            if is_specific_value_keyword(kw_clean):
                continue
            kw_norm = _normalize_for_dedup(kw_clean)
            if not kw_norm or kw_norm in seen_norm:
                continue
            if kw_norm in body_norm:
                valid.append(kw_clean)
                seen_norm.add(kw_norm)
        if valid:
            # LLM 关键词有效 → 覆盖
            s["gt_expected"] = {**gt_exp, "keywords": valid[:12]}
            mr = s.get("match_rules") or {}
            tb = dict(mr.get("trace_binding") or {})
            tb["output_keywords"] = valid[:6]
            mr["trace_binding"] = tb
            s["match_rules"] = mr
        else:
            # LLM 关键词无效（失败/无辨识力）→ 兜底：步骤名拆词 + 正文实体编号
            # 实体编号（INTEL/ENV/T-001/装备型号）在 agent 工具 input 中原样出现，辨识力强
            title = (s.get("name") or "").strip()
            fallback = _extract_fallback_keywords(title, body_text)
            if fallback:
                s["gt_expected"] = {**gt_exp, "keywords": fallback[:12]}
                mr = s.get("match_rules") or {}
                tb = dict(mr.get("trace_binding") or {})
                tb["output_keywords"] = fallback[:6]
                mr["trace_binding"] = tb
                s["match_rules"] = mr
    return steps
