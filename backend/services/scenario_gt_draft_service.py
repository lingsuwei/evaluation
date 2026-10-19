"""GT Markdown → 场景子步骤草稿（ST-2-3）。"""
from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional

# 标题正则：Markdown # 标题 / 一级数字编号(1. ) / 第N章|节|步
# 只匹配一级编号（如 "1. 标题"），不匹配 "1.1"、"1.1.1" 等多级子编号，
# 避免父步骤 body 被子步骤切走。
_HEADING_RE = re.compile(
    r"^(?:#{1,4}\s+|(?:\d+\.\s+)|(?:第[一二三四五六七八九十\d]+[章节步]\s*))(.+?)\s*$"
)
# 高级步骤标记：第N步/第N章/第N节/步骤N — 存在时纯数字编号(1.) 降级为子步骤
_HIGH_LEVEL_STEP_RE = re.compile(r"^(?:第[一二三四五六七八九十\d]+[章节步]|步骤\s*\d+)")
# 纯数字编号标题（无 # 前缀）：1. xxx
_DECIMAL_HEADING_RE = re.compile(r"^\d+\.\s+")
_BULLET_RE = re.compile(r"^[\s]*[-*•]\s+(.+)$")
_KEYWORD_INLINE_RE = re.compile(
    r"`([^`]+)`|「([^」]+)」|【([^】]+)】|(\b[A-Z][A-Z0-9_]{2,}\b)"
)


def _extract_keywords_from_block(lines: List[str]) -> List[str]:
    keywords: List[str] = []
    for line in lines:
        m = _BULLET_RE.match(line.strip())
        text = (m.group(1) if m else line).strip()
        if not text or len(text) < 2:
            continue
        for g in _KEYWORD_INLINE_RE.findall(text):
            for part in g:
                if part and part.strip():
                    keywords.append(part.strip())
        # 短句取前 12 字作为弱关键词（草稿用）
        compact = re.sub(r"\s+", "", text)
        if 2 <= len(compact) <= 24 and compact not in keywords:
            keywords.append(compact[:24])
    # 去重保序
    seen: set[str] = set()
    out: List[str] = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            out.append(kw)
    return out[:12]


def _normalize_for_dedup(s: str) -> str:
    """归一化字符串用于去重比较（去空格、去标点符号）。"""
    return re.sub(r"[\s\u3000:：，,。.、！!？?@#~\-→✓✅\[\]（）()【】]+", "", s or "")


# body 数据标志：结构化数据模式（业务编号 / 坐标对 / 表格行 / JSON）
# 注意：不匹配裸数字 \d，因为过渡话术里常含步骤编号引用（如"步骤1"、"步骤3至5"）
_DATA_FLAG_RE = re.compile(
    r"INTEL|ENV|DT-|R0\d|UAV|LM-|AMV|DOG|WRV|UGV"  # 业务编号
    r"|T-0\d"                                        # 目标编号
    r"|OPORD"                                        # 作战命令编号
    r"|\[\d+\.?\d*[,\s]+\d+\.?\d*\]"                 # 坐标对
    r"|\d+\.?\d*\s+[,，]\s*\d+\.?\d*"                # 逗号分隔数值对
    r"|[A-Z]{2,}-\d"                                 # 带编号的业务标识（WPN-HEAT 等）
    r"|[\[{]\s*[\"']?\w"                             # JSON/结构化数据开头
)

# 步骤切换指引词：body 中出现这些词，说明是过渡话术而非本步骤实质内容
_TRANSITION_WORDS: tuple = (
    "进入步骤", "转入步骤", "开始步骤", "继续步骤", "下一步",
    "进入下一步", "进入第", "开始执行", "即将开始",
)

# 具体值型关键词标志：公式/计算结果/具体数量/校验结果
# 这类 keyword 是 golden answer 的具体值，agent 输出格式稍异即 0 命中，
# 不应作为 keyword 模式的判分点（应走 llm_aggregate）
_SPECIFIC_VALUE_RE = re.compile(
    r"="                # 公式/结果（所需数量=3架、状态=已确认）
    r"|≥|≤|>{1,2}|<{1,2}"  # 比较运算
    r"|ceil\(|floor\(|sum\("  # 计算
    r"|\d+发|\d+架|\d+个|\d+辆"  # 具体数量
    r"|✅|✓"            # 校验结果
)


def is_specific_value_keyword(kw: str) -> bool:
    """判断 keyword 是否为具体值型（golden answer 的具体数值/公式结果）。

    这类 keyword 不适合用子串精确匹配，应过滤掉。
    """
    return bool(_SPECIFIC_VALUE_RE.search(kw))


# ── 结构性噪声关键词过滤（通用形态，非领域词表）──
# 作为 LLM 关键词提取的安全网，过滤掉无步骤辨识力的形态：
# 步骤编号/标题前缀、过渡话术、纯通用过程词。

# 步骤编号/标题前缀：步骤1 / 第一步 / SP1 / 子问题1
_STEP_PREFIX_RE = re.compile(
    r"^(?:"
    r"步骤\s*\d"
    r"|第[一二三四五六七八九十百\d]+步"
    r"|SP\s*\d"
    r"|子问题\s*\d"
    r")",
    re.I,
)
# 纯过渡话术：进入步骤 / 转入步骤 / 下一步 / 开始执行 / 即将开始
_TRANSITION_PREFIX_RE = re.compile(
    r"^(?:进入|转入|开始|继续)?(?:步骤|下一步|第[一二三四五六七八九十\d]+步)"
    r"|^(?:进入下一步|开始执行|即将开始)"
)
# 纯通用过程词（2-4字，无领域语义，每个步骤都可能出现，无辨识力）
_PURE_GENERIC_WORDS: frozenset[str] = frozenset({
    "步骤", "输出", "结果", "分析", "判断", "结论", "校验",
    "提取", "计算", "汇总", "评估", "推理", "推导", "验证",
    "检查", "确认", "获取", "生成", "处理",
})


def is_structural_noise_keyword(kw: str) -> bool:
    """判断关键词是否为结构性噪声（步骤编号/过渡话术/纯通用过程词）。

    通用形态过滤，不依赖任何业务领域词表。
    作为 LLM 关键词提取的安全网，过滤掉无步骤辨识力的形态。
    """
    s = (kw or "").strip()
    if len(s) < 2:
        return True
    if _STEP_PREFIX_RE.match(s):
        return True
    if _TRANSITION_PREFIX_RE.match(s):
        return True
    if s in _PURE_GENERIC_WORDS:
        return True
    return False


def is_noise_section(sec: Dict[str, Any]) -> bool:
    """判断段是否为噪声段（过渡性/无实质可评测内容）。

    仅对 body 非空的段做噪声判断；body 为空时内容在 title 中（如 N 模式单行步骤），
    不应过滤。

    三条件 AND（同时满足才判为噪声，避免误杀合理短步骤）：
    1. body 内容极少 — 非空行数 <= 2 且去标点后字符数 <= 30
    2. body 无数据标志 — 无业务编号/坐标/表格/JSON 等结构化数据
    3. body 含步骤切换指引词 或 keywords 退化（body 提取不出与 title 不同的有效 keyword）
    """
    body_lines = [ln for ln in sec.get("body") or [] if ln.strip()]
    if not body_lines:
        return False

    title = (sec.get("title") or "").strip()
    if not title:
        return False

    if len(body_lines) > 2:
        return False

    blob = "\n".join(body_lines)

    # 条件1：body 去标点后字符数 <= 30
    if len(_normalize_for_dedup(blob)) > 30:
        return False

    # 条件2：body 无数据标志
    if _DATA_FLAG_RE.search(blob):
        return False

    # 条件3：步骤切换指引词 或 keywords 退化
    has_transition = any(w in blob for w in _TRANSITION_WORDS)
    if has_transition:
        return True
    body_keywords = _extract_keywords_from_block(body_lines[:80])
    title_norm = _normalize_for_dedup(title)
    kw_degraded = not body_keywords or all(
        _normalize_for_dedup(kw) == title_norm for kw in body_keywords
    )
    return kw_degraded


def draft_steps_from_markdown(
    markdown: str,
    *,
    scenario_key: Optional[str] = None,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    将 GT Markdown 按标题切分为子步骤草稿。
    每个标题块下的列表项/段落用于生成 gt_criteria 与 gt_expected.keywords。
    """
    text = (markdown or "").strip()
    if not text:
        return {
            "scenario_key": scenario_key or f"draft_{uuid.uuid4().hex[:8]}",
            "name": name or "GT 草稿场景",
            "steps": [],
            "warnings": ["Markdown 为空"],
        }

    lines = text.splitlines()
    # 预扫描：是否存在高级步骤标记（第N步/步骤N/第N章/第N节）。
    # 存在时，纯数字编号行（1. xxx）降级为 body，不作为独立步骤标题。
    has_high_level = any(
        _HIGH_LEVEL_STEP_RE.match(line.strip())
        for line in lines
        if line.strip()
    )
    sections: List[Dict[str, Any]] = []
    current_title: Optional[str] = None
    current_body: List[str] = []

    def _flush() -> None:
        nonlocal current_title, current_body
        if current_title:
            sections.append({"title": current_title, "body": list(current_body)})
        current_title = None
        current_body = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_title:
                current_body.append("")
            continue
        # 存在高级标记时，纯数字编号行（非 # 标题）降级为 body
        if has_high_level and not stripped.startswith("#") and _DECIMAL_HEADING_RE.match(stripped):
            if current_title:
                current_body.append(line)
            continue
        hm = _HEADING_RE.match(stripped)
        if hm:
            _flush()
            current_title = hm.group(1).strip()
            current_body = []
            continue
        if current_title:
            current_body.append(line)

    _flush()

    if not sections:
        # 无标题：整篇作为单步
        sections = [{"title": name or "整体方案", "body": lines}]

    # 过滤噪声段（过渡性/无实质可评测内容）
    filtered = [sec for sec in sections if not is_noise_section(sec)]
    if filtered:
        sections = filtered
    # 全被过滤时保持原样（上层 draft_steps_from_model_answer 会因 steps 仍存在不兜底；
    # 但这些段已被判为噪声，保留它们比返回空更安全——至少有内容可评）

    steps: List[Dict[str, Any]] = []
    for idx, sec in enumerate(sections, start=1):
        body_lines = [ln for ln in sec["body"] if ln.strip()]
        criteria = []
        for ln in body_lines:
            bm = _BULLET_RE.match(ln.strip())
            criteria.append((bm.group(1) if bm else ln).strip()[:200])
        criteria = [c for c in criteria if c][:8]
        keywords = _extract_keywords_from_block(body_lines)
        # 过滤具体值型关键词（golden answer 的具体数值/公式结果），
        # 这类 keyword 不适合子串精确匹配，保留会导致系统性 0 命中
        # 同时过滤结构性噪声（步骤编号/过渡话术/纯通用过程词），领域无关
        keywords = [
            kw for kw in keywords
            if not is_specific_value_keyword(kw) and not is_structural_noise_keyword(kw)
        ]
        step_key = f"S{idx}"
        steps.append(
            {
                "step_key": step_key,
                "name": sec["title"],
                "step_order": idx,
                "weight": round(1.0 / max(len(sections), 1), 4),
                "required": True,
                "gt_criteria": criteria or [sec["title"]],
                "gt_expected": {"keywords": keywords},
                "match_rules": {
                    "trace_binding": {
                        "span_name_patterns": ["llm:main_iteration", "tool:"],
                        "output_keywords": keywords[:6],
                    }
                },
            }
        )

    sk = scenario_key or f"draft_{uuid.uuid4().hex[:8]}"
    return {
        "scenario_key": sk,
        "name": name or f"{sk}（GT 草稿）",
        "description": "由 GT Markdown 半自动抽取，需人工校对后入库",
        "steps": steps,
        "warnings": [] if steps else ["未能解析出子步骤"],
    }
