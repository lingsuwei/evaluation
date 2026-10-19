"""场景子步骤 LLM 二次汇聚服务。"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from logger import log

LLM_NOT_CONFIGURED_MSG = (
    "部分子步骤配置了「基于Prompt评测」，但未配置可用的 LLM API Key。"
    "请在「系统设置 → LLM 模型」中配置 API Key，或将相关步骤改回「Ground_Truth评测」。"
    "当前将跳过基于Prompt评测，仅按关键词判分。"
)

# ── 分块评测参数 ──────────────────────────────────────
# 触发分块的字符数阈值（约 2000 tokens，中文按 1.5 字/token 估算）
_CHUNK_THRESHOLD_CHARS = 3000
# 每块最大字符数
_CHUNK_MAX_CHARS = 3000
# 块之间重叠字符数（保持上下文连贯）
_CHUNK_OVERLAP_CHARS = 200

# ── checklist 参数 ────────────────────────────────────
# 触发 LLM 现提 checklist 的 body 长度阈值
_CHECKLIST_LLM_THRESHOLD_CHARS = 200
# checklist 最多保留项数
_CHECKLIST_MAX_ITEMS = 5
# gt 作为 example 时不截断的阈值
_EXAMPLE_FULL_TEXT_THRESHOLD = 2000
# gt 超长时截取的长度
_EXAMPLE_TRUNCATE_CHARS = 500
# 分块评测最大并发数
_CHUNK_MAX_CONCURRENCY = 3

# 是否启用分段评测（env 开关，默认 false 直接一次评测）
_CHUNK_ENABLED = os.getenv("SCENARIO_LLM_CHUNK_ENABLED", "false").strip().lower() in (
    "1", "true", "yes", "on",
)


async def llm_aggregate_step_output(
    step_output: str,
    aggregate_prompt: str,
    llm_model: Optional[str] = None,
) -> str:
    """
    对步骤 output 执行 LLM 二次汇聚，返回汇聚后的文本。

    Args:
        step_output: 从 Span 绑定提取的步骤 output 文本
        aggregate_prompt: 汇聚提示词（来自 match_rules.eval_config.aggregate_prompt）
        llm_model: 可选的 LLM 模型 key

    Returns:
        汇聚后的文本
    """
    from evaluators.llm_client import evaluate_with_llm

    if not step_output or not step_output.strip():
        return ""

    if not is_llm_configured(llm_model):
        log(
            "WARNING",
            "SCENARIO_LLM_AGG",
            "LLM 未配置，跳过二次汇聚",
            llm_model=llm_model,
        )
        return ""

    prompt = f"""{aggregate_prompt}

【Agent 推理输出】
{step_output}

请严格按照上述要求对 Agent 推理输出进行汇聚，直接输出汇聚结果，不要添加多余的解释。"""

    result = await evaluate_with_llm(
        prompt,
        system_prompt="你是一个专业的信息抽取与汇聚助手。请严格按照用户要求对内容进行结构化汇总。",
        model_key=llm_model,
    )

    log(
        "INFO",
        "SCENARIO_LLM_AGG",
        "LLM 二次汇聚完成",
        prompt_preview=aggregate_prompt[:80],
        input_length=len(step_output),
        output_length=len(result) if result else 0,
    )

    return result or ""


async def llm_judge_step_output(
    step_output: str,
    aggregate_prompt: str,
    gt_expected: Optional[Dict[str, Any]] = None,
    llm_model: Optional[str] = None,
) -> Tuple[float, bool, Dict[str, Any]]:
    """
    LLM 直接评判步骤输出（替代二次汇聚 + 关键词判分）。

    当 step_output 超过阈值时自动分块评测，加权汇聚得分。

    逻辑：
    - 若 gt_expected.keywords 有值：LLM 参考关键词 + 用户提示词综合评判
    - 若 gt_expected.keywords 为空：LLM 仅根据用户提示词自由评判
    - 若 step_output 过长：按自然段分块，逐块评测后加权平均

    Returns:
        Tuple[float, bool, Dict]: (score, passed, evidence)
    """
    from evaluators.llm_client import evaluate_with_llm

    if not step_output or not step_output.strip():
        return 0.0, False, {"reason": "llm_judge_no_input", "score_group": "llm_judge"}

    if not is_llm_configured(llm_model):
        raise RuntimeError("LLM API 未配置")

    gt_keywords = step_gt_keywords(gt_expected)
    # 关键词过多时截断，避免 prompt 过长导致 LLM 返回异常
    if len(gt_keywords) > 200:
        gt_keywords = gt_keywords[:200]
    gt_text = _format_ground_truth_for_prompt(gt_expected)

    # ── checklist 提取 ──
    # 有 body 路径：长 body≥200字 → LLM 现提；短 body<200字 → 按句拆
    # 无 body 路径：用 criteria 作 checklist（由调用方注入 gt_expected.criteria）
    checklist_items: Optional[List[str]] = None
    gt_body = ""
    if isinstance(gt_expected, dict):
        gt_body = (gt_expected.get("text") or "").strip()
    if gt_body:
        if len(gt_body) >= _CHECKLIST_LLM_THRESHOLD_CHARS:
            checklist_items = await _extract_checklist_from_body_llm(
                gt_body, llm_model
            )
            if checklist_items is None:
                # LLM 提炼失败，回退按句拆
                checklist_items = _split_short_body_to_checklist(gt_body)
        else:
            checklist_items = _split_short_body_to_checklist(gt_body)
    # example_text 用 gt_text（_format_ground_truth_for_prompt 格式化后的全文）
    example_text = gt_text if gt_text else ""

    # 未启用分段评测时，直接一次评测
    if not _CHUNK_ENABLED:
        return await _judge_single_chunk(
            step_output, aggregate_prompt, gt_keywords, gt_text, llm_model,
            checklist_items=checklist_items,
            example_text=example_text,
        )

    # 启用分段评测：判断是否需要分块
    chunks = _split_text_into_chunks(step_output)
    if len(chunks) <= 1:
        # 短文本：直接单次评测
        return await _judge_single_chunk(
            step_output, aggregate_prompt, gt_keywords, gt_text, llm_model,
            checklist_items=checklist_items,
            example_text=example_text,
        )

    # 长文本：分块评测 + 加权汇聚
    log(
        "INFO",
        "SCENARIO_LLM_AGG",
        "文本超长，启用分块评测",
        total_length=len(step_output),
        chunk_count=len(chunks),
    )

    semaphore = asyncio.Semaphore(_CHUNK_MAX_CONCURRENCY)

    async def _eval_chunk(idx: int, chunk: str) -> Tuple[int, str, Tuple[float, bool, Dict]]:
        async with semaphore:
            result = await _judge_single_chunk(
                chunk, aggregate_prompt, gt_keywords, gt_text, llm_model,
                chunk_index=idx + 1,
                chunk_total=len(chunks),
                checklist_items=checklist_items,
                example_text=example_text,
            )
            return idx, chunk, result

    tasks = [_eval_chunk(i, c) for i, c in enumerate(chunks)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 汇聚评分
    total_weight = 0.0
    weighted_score = 0.0
    all_passed = True
    reasons: List[str] = []
    for item in results:
        if isinstance(item, Exception):
            log("WARNING", "SCENARIO_LLM_AGG", "分块评测失败", error=str(item))
            continue
        idx, chunk, (score, passed, evidence) = item
        weight = float(len(chunk))
        weighted_score += score * weight
        total_weight += weight
        if not passed:
            all_passed = False
        reason = evidence.get("reason", "")
        if reason:
            reasons.append(f"[段{idx+1}] {reason}")

    if total_weight == 0:
        return 0.0, False, {
            "reason": "所有分块评测均失败",
            "score_group": "llm_judge",
            "eval_source": "llm_chunked",
        }

    final_score = round(weighted_score / total_weight, 4)
    final_passed = all_passed and final_score >= 0.6
    combined_reason = "；".join(reasons[:3]) if reasons else "分块评测完成"
    if len(reasons) > 3:
        combined_reason += f"（共 {len(reasons)} 段评语）"

    return final_score, final_passed, {
        "reason": combined_reason[:200],
        "matched_keywords": gt_keywords if final_passed else [],
        "match_ratio": final_score,
        "score_group": "llm_judge",
        "eval_source": "llm_chunked",
        "chunk_count": len(chunks),
        "chunk_scores": [
            {"chunk": idx + 1, "score": r[2][0], "passed": r[2][1]}
            for r in results
            if not isinstance(r, Exception)
            for idx in [r[0]]
        ],
    }


def _split_short_body_to_checklist(body: str) -> List[str]:
    """短 body（<200字）按句号/分号/换行拆成检查项，过滤空行和表头。

    复用 scenario_model_answer_draft_service._clean_criterion_line 的过滤逻辑。
    最多保留 _CHECKLIST_MAX_ITEMS 项。
    """
    from services.scenario_model_answer_draft_service import _clean_criterion_line

    if not body or not body.strip():
        return []
    # 按句号/分号/换行拆分
    raw_items = re.split(r"[。\n；;]", body)
    items: List[str] = []
    for raw in raw_items:
        cleaned = _clean_criterion_line(raw)
        if cleaned and cleaned not in items:
            items.append(cleaned)
        if len(items) >= _CHECKLIST_MAX_ITEMS:
            break
    return items


async def _extract_checklist_from_body_llm(
    body: str, llm_model: Optional[str]
) -> Optional[List[str]]:
    """长 body（≥200字）用 LLM 现提 checklist（纯字符串数组，最多5项）。

    用任务模型提取，不跟随 run.inference_model_id。
    提炼失败返回 None，调用方跳过 checklist。
    """
    from evaluators.llm_client import evaluate_with_llm

    truncated = body[:_CHUNK_MAX_CHARS]
    prompt = f"""请从以下参考答案中提炼出最多 {_CHECKLIST_MAX_ITEMS} 个关键评测点，用于评估 Agent 输出是否覆盖了这些核心信息。

【参考答案】
{truncated}

【要求】
- 每个评测点是一句简短的陈述，描述 Agent 输出应当包含的关键信息
- 优先提炼数值、实体、必含信息等可验证的点
- 输出纯 JSON 字符串数组，不要添加解释或 markdown 标记
- 示例：["包含物料A的单价450元", "列出所有UAV编号", "给出单架次用量"]"""

    try:
        result = await evaluate_with_llm(
            prompt,
            system_prompt="你是一个信息提炼助手。直接输出 JSON 字符串数组，不要添加解释。",
            model_key=llm_model,
            override_temperature=0,
        )
        if not result or not result.strip():
            return None
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
            cleaned = re.sub(r'\s*```$', '', cleaned)
        data = json.loads(cleaned)
        if isinstance(data, list):
            items = [str(item).strip() for item in data if str(item).strip()]
            return items[:_CHECKLIST_MAX_ITEMS] if items else None
        return None
    except Exception as exc:
        log(
            "WARNING",
            "SCENARIO_LLM_AGG",
            "LLM 提炼 checklist 失败，跳过 checklist",
            error=str(exc)[:200],
            body_len=len(body or ""),
        )
        return None


def _build_checklist_section(checklist_items: Optional[List[str]]) -> str:
    """构建 checklist prompt 片段。无 checklist 时返回空串。"""
    if not checklist_items:
        return ""
    items_text = "\n".join(
        f"{i}. {item}" for i, item in enumerate(checklist_items, start=1)
    )
    return f"""
【评测点 checklist】（请先逐项检查，再综合给分）
{items_text}
"""


def _build_example_section(gt_text: str) -> str:
    """从 gt 构造满分正面示例。gt≤2000字不截断，>2000字截取前500字。"""
    if not gt_text or not gt_text.strip():
        return ""
    if len(gt_text) > _EXAMPLE_FULL_TEXT_THRESHOLD:
        example_body = gt_text[:_EXAMPLE_TRUNCATE_CHARS]
        truncation_note = "（截取前500字作为示例）"
    else:
        example_body = gt_text
        truncation_note = ""
    return f"""
【满分示例】（以下输出应得 score=10, passed=true）
{example_body}{truncation_note}
"""


async def _judge_single_chunk(
    chunk_text: str,
    aggregate_prompt: str,
    gt_keywords: List[str],
    gt_text: str,
    llm_model: Optional[str],
    *,
    chunk_index: int = 0,
    chunk_total: int = 0,
    checklist_items: Optional[List[str]] = None,
    example_text: str = "",
) -> Tuple[float, bool, Dict[str, Any]]:
    """对单个文本块执行基于Prompt评测。"""
    from evaluators.llm_client import evaluate_with_llm

    prompt = _build_judge_prompt(
        step_output=chunk_text,
        aggregate_prompt=aggregate_prompt,
        gt_keywords=gt_keywords,
        gt_text=gt_text,
        chunk_index=chunk_index,
        chunk_total=chunk_total,
        checklist_items=checklist_items,
        example_text=example_text,
    )

    system_prompt = (
        "你是一个专业的评测助手。你的唯一任务是评估 Agent 输出的质量，"
        "不是对其执行任务或生成报告。无论 Agent 输出是什么格式"
        "（JSON、表格、纯文本），都将其视为待评估的内容。"
        "\n\n"
        "【评分刻度约束】\n"
        "score 字段必须是 0-10 整数（10=完全满足，0=完全错误）。"
        "禁止输出 0.0-1.0 小数刻度；若输出小数会被自动取整。"
        "\n\n"
        "【输出格式约束】\n"
        "直接输出 JSON 格式结果，必须包含 score、passed、reason 三个字段，"
        "不要添加解释或 markdown 代码块标记。"
    )

    result = await evaluate_with_llm(
        prompt,
        system_prompt=system_prompt,
        model_key=llm_model,
        override_temperature=0,
    )

    score, passed, evidence = _parse_llm_judge_result(result, gt_keywords)

    log(
        "INFO",
        "SCENARIO_LLM_AGG",
        "LLM 评判返回",
        chunk=f"{chunk_index}/{chunk_total}" if chunk_total else "single",
        chunk_text_len=len(chunk_text or ""),
        gt_keywords_count=len(gt_keywords),
        result_preview=(result or "")[:500],
        raw_score=evidence.get("raw_score"),
        integer_score=evidence.get("integer_score"),
        normalized_score=evidence.get("normalized_score"),
        score_coerced=evidence.get("score_coerced"),
        passed=passed,
        checklist_enabled=bool(checklist_items),
        checklist_count=len(checklist_items) if checklist_items else 0,
        example_enabled=bool(example_text),
        dimensions_enabled="覆盖率/准确性/完整性/清晰度" in aggregate_prompt,
    )

    return score, passed, evidence


def _format_ground_truth_for_prompt(gt_expected: Optional[Dict[str, Any]]) -> str:
    """将 gt_expected 格式化为可读提示文本，包含 reference_files 中的 JSON 数据。"""
    if not gt_expected:
        return ""
    parts = []
    if gt_expected.get("keywords"):
        parts.append(f"关键词：{', '.join(gt_expected['keywords'])}")
    if gt_expected.get("required_cmd_ids"):
        parts.append(f"必须出现的命令ID：{', '.join(gt_expected['required_cmd_ids'])}")
    if gt_expected.get("alt_keywords"):
        parts.append(f"备选关键词：{', '.join(gt_expected['alt_keywords'])}")
    # reference_files: 上传的文件内容（JSON 格式）
    ref_files = gt_expected.get("reference_files")
    if isinstance(ref_files, dict) and ref_files:
        for fname, fdata in ref_files.items():
            if isinstance(fdata, list):
                # CSV → JSON 数组
                parts.append(f"参考文件 {fname}（表格数据）：{json.dumps(fdata, ensure_ascii=False)}")
            elif isinstance(fdata, dict):
                # TXT → JSON 对象
                content = fdata.get("content", "")
                if content:
                    parts.append(f"参考文件 {fname}：{content}")
                else:
                    parts.append(f"参考文件 {fname}：{json.dumps(fdata, ensure_ascii=False)}")
    return "\n".join(parts)


def _split_text_into_chunks(
    text: str,
    *,
    max_chars: int = _CHUNK_MAX_CHARS,
    overlap: int = _CHUNK_OVERLAP_CHARS,
) -> List[str]:
    """将长文本按自然段分块。

    优先按段落分割，段落过长时按句子分割，确保每块不超过 max_chars。
    块之间保留 overlap 字符的上下文重叠。

    Returns:
        分块列表。若文本未超阈值则返回单元素列表 [text]。
    """
    if not text or not text.strip():
        return []
    text = text.strip()
    if len(text) <= _CHUNK_THRESHOLD_CHARS:
        return [text]

    chunks: List[str] = []
    # 优先按双换行（段落）分割
    paragraphs = re.split(r'\n\s*\n', text)
    current_chunk = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # 段落本身超长，按句子再分
        if len(para) > max_chars:
            sentences = re.split(r'(?<=[。！？；\.\!\?;])\s*', para)
            for sent in sentences:
                sent = sent.strip()
                if not sent:
                    continue
                # 句子仍超长，硬切
                while len(sent) > max_chars:
                    if current_chunk:
                        chunks.append(current_chunk)
                        current_chunk = ""
                    chunks.append(sent[:max_chars])
                    sent = sent[max_chars - overlap:]
                if not sent:
                    continue
                if len(current_chunk) + len(sent) + 1 > max_chars:
                    if current_chunk:
                        chunks.append(current_chunk)
                    current_chunk = sent
                else:
                    current_chunk = f"{current_chunk}\n{sent}" if current_chunk else sent
        else:
            if len(current_chunk) + len(para) + 2 > max_chars:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = para
            else:
                current_chunk = f"{current_chunk}\n\n{para}" if current_chunk else para

    if current_chunk:
        chunks.append(current_chunk)

    return chunks if chunks else [text]


def _build_judge_prompt(
    step_output: str,
    aggregate_prompt: str,
    gt_keywords: List[str],
    gt_text: str,
    *,
    chunk_index: int = 0,
    chunk_total: int = 0,
    checklist_items: Optional[List[str]] = None,
    example_text: str = "",
) -> str:
    """构建评测提示词。

    两种评测路径：
    - keyword 模式（Ground_Truth评测）：需要标准答案关键词，用关键词匹配评分（不走此函数）
    - llm_aggregate 模式（基于Prompt评测）：使用基于Prompt评测评分，标准答案仅作参考，不是必须的

    分块评测时，在 prompt 中标注当前块的序号和总数。
    checklist_items 注入评测点 checklist（引导但不决定 score）。
    example_text 注入满分正面示例（从 gt 构造）。
    """
    # 标准答案仅作参考，不是必须的
    if gt_keywords:
        gt_section = f"""
【标准答案参考】（可选参考，非必须完全匹配）
{', '.join(gt_keywords)}"""
    elif gt_text:
        gt_section = f"""
【标准答案参考】（可选参考，非必须完全匹配）
{gt_text}"""
    else:
        gt_section = """
【标准答案参考】
本题未提供标准答案。请根据上述「评测标准」对 Agent 输出进行严格评判。
注意：没有标准答案不等于默认通过，必须逐一检查评测标准中的每条要求。"""

    chunk_note = ""
    if chunk_total > 1:
        chunk_note = f"""
【评测范围提示】
当前评测的是 Agent 输出的第 {chunk_index}/{chunk_total} 段。请仅针对本段内容评分。
如果评测标准涉及的要点不在本段中，不要扣分（可能在其他段中已覆盖）。"""

    checklist_section = _build_checklist_section(checklist_items)
    example_section = _build_example_section(example_text)

    return f"""【评测标准】（由用户自定义）
{aggregate_prompt}
{gt_section}{chunk_note}

【Agent 输出内容】
{step_output}
{example_section}
【通用评分尺度】（本尺度优先于评测标准中的任何刻度描述）
请按 0-10 整数刻度评分（不要输出 0.0-1.0 小数）：
- 10：完全满足所有评测点，内容准确完整
- 9：满足全部评测点，仅表述有极微小瑕疵
- 8：缺 1 项次要内容，其余全部满足
- 7：缺 2 项次要内容，或 1 项边缘内容
- 6：缺 1 项关键内容，主要评测点仍命中
- 5：缺 1 项关键内容 + 1 项次要内容
- 4：缺多数关键评测点，仅部分命中
- 3：仅少量关键评测点命中，多数缺失
- 2：仅边缘相关，关键内容几乎全部缺失
- 1：几乎不相关，仅存在极少相关内容
- 0：完全错误或与要求无关
{checklist_section}
【输出要求】
请先按上述评测标准完成所有评测任务，然后在 JSON 末尾追加以下三个汇总字段：
- "score": 0-10 整数（不接受 0.0-1.0 小数，小数会被取整）
- "passed": true 或 false，表示总体是否通过
- "reason": "评分理由（80字左右），必须说明扣分原因"

必须同时包含 score、passed、reason 三个字段，缺少任何一个都将被视为无效响应。
请输出严格 JSON 格式（不允许添加解释或 markdown 代码块标记）。"""


def _coerce_integer_score(raw: Any) -> Tuple[int, bool]:
    """将 LLM 输出的 score 字段强转为 0-10 整数。

    返回 (coerced_int, was_coerced)：
    - 整数/整数字符串直接用
    - 浮点 round 后取整（8.5 → 9）
    - "0.8" 等浮点字符串（LLM 误用 0-1 小数）→ ×10 round → 8
    - 超范围 clamp 到 [0, 10]
    was_coerced=True 表示发生了非平凡的转换，便于日志诊断
    """
    was_coerced = False
    try:
        if isinstance(raw, str):
            s = raw.strip()
            # 纯整数串
            if re.fullmatch(r"-?\d+", s):
                val = int(s)
            else:
                val = float(s)
                was_coerced = True
        elif isinstance(raw, bool):
            val = 1 if raw else 0
            was_coerced = True
        elif isinstance(raw, int):
            val = raw
        elif isinstance(raw, float):
            val = raw
            was_coerced = True
        else:
            return 0, True
    except (TypeError, ValueError):
        return 0, True

    # 0-1 小数（LLM 误用旧浮点刻度）→ ×10 转换
    if isinstance(val, float) and 0.0 <= val <= 1.0 and val != 0.0:
        val = val * 10
        was_coerced = True

    # 四舍五入（Python3 round() 是银行家舍入，8.5→8 而非 9，这里需要标准四舍五入）
    int_val = int(val + 0.5) if val >= 0 else int(val - 0.5)
    if int_val < 0:
        int_val = 0
        was_coerced = True
    elif int_val > 10:
        int_val = 10
        was_coerced = True
    return int_val, was_coerced


def _parse_llm_judge_result(
    raw_result: str,
    gt_keywords: List[str],
) -> Tuple[float, bool, Dict[str, Any]]:
    """解析 LLM 返回的 JSON 评分结果。

    LLM 按新刻度输出 0-10 整数，这里归一为 0.0-1.0 浮点（int / 10.0）存储。
    非整数输入会被 round + clamp 到 [0, 10]。
    """
    if not raw_result or not raw_result.strip():
        return 0.0, False, {
            "reason": "LLM 返回为空，回退关键词判分",
            "matched_keywords": [],
            "match_ratio": 0.0,
            "score_group": "llm_judge",
            "eval_source": "llm",
            "raw_score": None,
            "normalized_score": 0.0,
        }
    parsed_data: Optional[Dict[str, Any]] = None
    try:
        # 去除 markdown 代码块标记
        cleaned = raw_result.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
            cleaned = re.sub(r'\s*```$', '', cleaned)
        parsed_data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError, KeyError):
        pass
    if parsed_data is None:
        # 尝试提取第一个完整 JSON 对象（支持嵌套）
        try:
            depth = 0
            start = -1
            for i, ch in enumerate(raw_result):
                if ch == '{':
                    if depth == 0:
                        start = i
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0 and start >= 0:
                        parsed_data = json.loads(raw_result[start:i + 1])
                        break
        except (json.JSONDecodeError, ValueError, KeyError):
            pass
    if not isinstance(parsed_data, dict):
        return 0.0, False, {
            "reason": "LLM 评分解析失败，回退关键词判分",
            "matched_keywords": [],
            "match_ratio": 0.0,
            "score_group": "llm_judge",
            "eval_source": "llm",
            "raw_score": None,
            "normalized_score": 0.0,
        }

    raw_score = parsed_data.get("score", 0)
    int_score, was_coerced = _coerce_integer_score(raw_score)
    normalized = round(int_score / 10.0, 4)
    passed = bool(parsed_data.get("passed", False))
    reason = str(parsed_data.get("reason", ""))
    return normalized, passed, {
        "reason": reason,
        "matched_keywords": gt_keywords if passed else [],
        "match_ratio": normalized,
        "score_group": "llm_judge",
        "eval_source": "llm",
        "raw_score": raw_score,
        "normalized_score": normalized,
        "integer_score": int_score,
        "score_coerced": was_coerced,
    }


def is_llm_configured(model_key: Optional[str] = None) -> bool:
    """当前环境是否已配置可用的 LLM API Key（数据库或 .env）。"""
    from config import get_llm_config

    cfg = get_llm_config(model_key)
    return bool((cfg.get("api_key") or "").strip())


def collect_llm_aggregate_warnings_for_templates(
    templates: List[Dict[str, Any]],
) -> List[str]:
    """扫描模板中启用的 llm_aggregate 步骤，返回配置/LLM 就绪性警告。"""
    config_issues: List[str] = []
    llm_missing_labels: List[str] = []
    for tpl in templates or []:
        for step in tpl.get("steps") or []:
            config_issues.extend(collect_llm_aggregate_step_config_issues(step))
            if not is_llm_aggregate_step(step):
                continue
            if not llm_aggregate_step_config_ready(step):
                continue
            eval_config = parse_eval_config(step.get("match_rules"))
            if is_llm_configured(eval_config.get("llm_model")):
                continue
            label = str(step.get("name") or step.get("step_key") or "未命名步骤").strip()
            if label and label not in llm_missing_labels:
                llm_missing_labels.append(label)

    warnings: List[str] = []
    if config_issues:
        shown = "；".join(config_issues[:8])
        if len(config_issues) > 8:
            shown = f"{shown} 等 {len(config_issues)} 项"
        warnings.append(
            f"以下基于Prompt评测步骤配置不完整：{shown}。"
            "请在评测集管理中补全Prompt。"
        )
    if llm_missing_labels:
        shown = "、".join(llm_missing_labels[:5])
        if len(llm_missing_labels) > 5:
            shown = f"{shown} 等 {len(llm_missing_labels)} 个步骤"
        warnings.append(f"{LLM_NOT_CONFIGURED_MSG}（涉及：{shown}）")
    return warnings


async def preview_templates_for_task_runs(
    session,
    runs: List[Any],
    *,
    forced_scenario_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """打分前预读任务将用到的场景模板（用于 LLM 就绪性检查）。"""
    from services.scenario_eval_service import get_template_by_key, resolve_scenario_for_run

    templates: List[Dict[str, Any]] = []
    seen: set[str] = set()
    if forced_scenario_key:
        tpl = await get_template_by_key(session, forced_scenario_key)
        if tpl:
            templates.append(tpl)
        return templates
    for run in runs or []:
        key = await resolve_scenario_for_run(session, run.id)
        if not key or key in seen:
            continue
        seen.add(key)
        tpl = await get_template_by_key(session, key)
        if tpl:
            templates.append(tpl)
    return templates


async def collect_llm_aggregate_warnings_for_task(
    session,
    runs: List[Any],
    *,
    forced_scenario_key: Optional[str] = None,
) -> List[str]:
    templates = await preview_templates_for_task_runs(
        session,
        runs,
        forced_scenario_key=forced_scenario_key,
    )
    return collect_llm_aggregate_warnings_for_templates(templates)


def parse_eval_config(match_rules: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从 match_rules 中解析 eval_config，返回带默认值的结构。"""
    if not match_rules or not isinstance(match_rules, dict):
        return {"mode": "keyword", "aggregate_prompt": "", "llm_model": None}
    config = match_rules.get("eval_config") or {}
    if not isinstance(config, dict):
        return {"mode": "keyword", "aggregate_prompt": "", "llm_model": None}
    return {
        "mode": (config.get("mode") or "keyword"),
        "aggregate_prompt": (config.get("aggregate_prompt") or "").strip(),
        "llm_model": config.get("llm_model"),
    }


def step_gt_keywords(gt_expected: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(gt_expected, dict):
        return []
    raw = gt_expected.get("keywords") or []
    if not isinstance(raw, list):
        return []
    return [str(k).strip() for k in raw if str(k).strip()]


def is_llm_aggregate_step(
    step: Dict[str, Any],
    *,
    require_enabled: bool = True,
) -> bool:
    if require_enabled and not step.get("enabled", True):
        return False
    eval_config = parse_eval_config(step.get("match_rules"))
    return eval_config["mode"] == "llm_aggregate"


def validate_llm_aggregate_step(
    *,
    step_key: str,
    name: str,
    enabled: bool = True,
    gt_expected: Optional[Dict[str, Any]] = None,
    match_rules: Optional[Dict[str, Any]] = None,
) -> None:
    """保存子步骤时校验 llm_aggregate 模式必填项。"""
    if not enabled:
        return
    eval_config = parse_eval_config(match_rules)
    if eval_config["mode"] != "llm_aggregate":
        return
    label = (name or step_key or "未命名步骤").strip()
    if not eval_config["aggregate_prompt"]:
        raise ValueError(
            f"步骤「{label}」为基于Prompt评测模式，Prompt不能为空"
        )


def collect_llm_aggregate_step_config_issues(step: Dict[str, Any]) -> List[str]:
    """运行时扫描单步 llm_aggregate 配置缺口（不抛异常）。"""
    if not is_llm_aggregate_step(step):
        return []
    label = str(step.get("name") or step.get("step_key") or "未命名步骤").strip()
    eval_config = parse_eval_config(step.get("match_rules"))
    issues: List[str] = []
    if not eval_config["aggregate_prompt"]:
        issues.append(f"步骤「{label}」缺少Prompt")
    return issues


def llm_aggregate_step_config_ready(step: Dict[str, Any]) -> bool:
    return not collect_llm_aggregate_step_config_issues(step)
