"""评测集 JSON / CSV / XLSX 导入导出（库内仍存 JSON 文本）。"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from schemas import DatasetItem

# 导出表头（英文，与界面展示字段对齐）与对应字段 key
EXPORT_CSV_HEADERS: List[str] = [
    "ID",
    "Question",
    "Prompt",
    "Ground_Truth",
    "Model_Answer",
    "Judging_Mode",
]
EXPORT_FIELD_KEYS: List[str] = [
    "id",
    "question",
    "prompt",
    "ground_truth",
    "model_answer",
    "judging_mode",
]

# 表头别名 → 标准字段
HEADER_ALIASES: Dict[str, str] = {
    "id": "id",
    "dataset_item_id": "id",
    "question": "question",
    "题目": "question",
    "input": "question",
    "query": "question",
    "prompt": "prompt",
    "提示词": "prompt",
    "ground_truth": "ground_truth",
    "标准答案": "ground_truth",
    "expected_output": "ground_truth",
    "model_answer": "model_answer",
    "模型答案": "model_answer",
    "answer": "model_answer",
    "mode_answer": "model_answer",
    "judging_mode": "judging_mode",
    "评测模式": "judging_mode",
    "context": "context",
    "上下文": "context",
    "scenario_key": "scenario_key",
    "scenario": "scenario_key",
    "场景": "scenario_key",
}


class DatasetUploadFormatError(ValueError):
    """无法识别的上传格式或 CSV 结构错误。"""


def detect_upload_format(filename: str) -> str:
    lower = (filename or "").lower().strip()
    if lower.endswith(".csv"):
        return "csv"
    if lower.endswith(".json"):
        return "json"
    if lower.endswith(".xlsx"):
        return "xlsx"
    raise DatasetUploadFormatError("仅支持 .json、.csv 或 .xlsx 文件")


def default_dataset_name(name: Optional[str], filename: Optional[str], dataset_id: str) -> str:
    if name and str(name).strip():
        return str(name).strip()
    if filename:
        base = filename.rsplit("/", 1)[-1]
        for ext in (".json", ".csv"):
            if base.lower().endswith(ext):
                base = base[: -len(ext)]
                break
        if base.strip():
            return base.strip()
    return f"dataset_{dataset_id}"


async def ensure_unique_dataset_name(session, base_name: str) -> str:
    """确保评测集名称在数据库中唯一，若重复则自动追加编号（_2, _3, ...）。

    匹配逻辑：精确匹配 base_name 本身以及 base_name_{数字} 的记录，
    避免误匹配（如 langfuse_A_B 不会匹配 langfuse_A 的编号）。
    """
    from database import Dataset
    from sqlalchemy import select

    result = await session.execute(
        select(Dataset.name).where(
            (Dataset.name == base_name) | (Dataset.name.like(f"{base_name}_%"))
        )
    )
    existing_names = {row[0] for row in result.all()}

    if base_name not in existing_names:
        return base_name

    # 找到 base_name_N 中最大的 N
    max_seq = 1
    _pattern = re.compile(rf"^{re.escape(base_name)}_(\d+)$")
    for name in existing_names:
        m = _pattern.match(name)
        if m:
            max_seq = max(max_seq, int(m.group(1)))

    return f"{base_name}_{max_seq + 1}"


def _normalize_header(header: str) -> Optional[str]:
    key = (header or "").strip().lower()
    if not key:
        return None
    return HEADER_ALIASES.get(key)


def _build_items_from_mapped_rows(
    rows: list,
    field_map: dict,
    *,
    col_by_idx: bool = False,
    start_line: int = 2,
) -> List[Dict[str, Any]]:
    """从已映射的 CSV/XLSX 行构建 items 列表（非场景模式）。"""
    items: List[Dict[str, Any]] = []
    errors: List[str] = []

    for line_no, row in enumerate(rows, start=start_line):
        if not row:
            continue
        if col_by_idx:
            # XLSX: row 是列表，通过列索引映射
            if not any(v.strip() for v in row):
                continue
            item: Dict[str, Any] = {}
            for col_idx, value in enumerate(row):
                canonical = field_map.get(col_idx)
                if not canonical:
                    continue
                cell = (value or "").strip()
                if cell:
                    item[canonical] = cell
        else:
            # CSV: row 是 dict
            if not any((v or "").strip() for v in row.values()):
                continue
            item = {}
            for raw_key, value in row.items():
                canonical = field_map.get(raw_key)
                if not canonical:
                    continue
                cell = (value or "").strip()
                if cell:
                    item[canonical] = cell

        if not item.get("question"):
            errors.append(f"第 {line_no} 行: 题目不能为空")
            continue
        if "model_answer" not in item:
            item["model_answer"] = ""
        items.append(item)

    if errors:
        raise DatasetUploadFormatError("\n".join(errors[:20]))
    if not items:
        raise DatasetUploadFormatError("无有效数据行")
    return items


def _build_scenario_from_mapped_rows(
    rows: list,
    field_map: dict,
    *,
    col_by_idx: bool = False,
    start_line: int = 2,
    format_label: str = "XLSX",
) -> Dict[str, Any]:
    """从已映射的 CSV/XLSX 行构建场景 items + steps 结构。"""
    items: List[Dict[str, Any]] = []
    steps: List[Dict[str, Any]] = []
    current_question: Optional[Dict[str, Any]] = None
    question_index_by_question: Dict[str, int] = {}

    for line_no, row in enumerate(rows, start=start_line):
        if not row:
            continue
        if col_by_idx:
            if not any(v.strip() for v in row):
                continue
            mapped: Dict[str, str] = {}
            for col_idx, value in enumerate(row):
                canonical = field_map.get(col_idx)
                if not canonical:
                    continue
                mapped[canonical] = (value or "").strip()
        else:
            if not any((v or "").strip() for v in row.values()):
                continue
            mapped = {}
            for raw_key, value in row.items():
                canonical = field_map.get(raw_key)
                if not canonical:
                    continue
                mapped[canonical] = (value or "").strip()

        q_text = mapped.get("question", "")
        if q_text:
            if q_text in question_index_by_question:
                current_question = items[question_index_by_question[q_text]]
            else:
                current_question = {
                    "question": q_text,
                    "prompt": mapped.get("prompt", ""),
                    "ground_truth": mapped.get("ground_truth", ""),
                    "model_answer": mapped.get("model_answer", ""),
                    "judging_mode": mapped.get("judging_mode", "Ground_Truth评测"),
                }
                question_index_by_question[q_text] = len(items)
                items.append(current_question)

        if current_question is None:
            raise DatasetUploadFormatError(
                f"第 {line_no} 行: 缺少题目信息（首行须包含题目列）"
            )

        step_q = mapped.get("step_question", "")
        if step_q:
            step = {
                "question_index": question_index_by_question[current_question["question"]],
                "step_question": step_q,
                "step_prompt": mapped.get("step_prompt", ""),
                "step_ground_truth": mapped.get("step_ground_truth", ""),
                "step_judging_mode": mapped.get("step_judging_mode", "Ground_Truth评测"),
                "step_model_answer": mapped.get("step_model_answer", ""),
                "step_weight": mapped.get("step_weight", ""),
            }
            steps.append(step)

    if not items:
        raise DatasetUploadFormatError(f"{format_label} 无有效数据行")
    if not steps:
        raise DatasetUploadFormatError(f"{format_label} 无有效步骤数据行（需包含 Step_Question 列）")

    return {
        "items": items,
        "scenario_steps": steps,
        "questions_count": len(items),
        "steps_count": len(steps),
    }


def parse_json_upload(content: bytes) -> List[Dict[str, Any]]:
    try:
        text = content.decode("utf-8-sig")
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DatasetUploadFormatError("Invalid JSON format") from exc
    if not isinstance(data, list):
        raise DatasetUploadFormatError("Dataset must be a JSON array")
    return data


def parse_csv_upload(content: bytes) -> List[Dict[str, Any]]:
    # UTF-8 优先，失败则静默降级 GBK（不提示用户，自动处理）
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("gbk")
        except UnicodeDecodeError as exc:
            raise DatasetUploadFormatError("CSV 编码无法识别，请使用 UTF-8 或 GBK 编码") from exc

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise DatasetUploadFormatError("CSV 缺少表头行")

    field_map: Dict[str, str] = {}
    for raw in reader.fieldnames:
        canonical = _normalize_header(raw)
        if canonical:
            field_map[raw] = canonical

    if "question" not in field_map.values():
        raise DatasetUploadFormatError("CSV 表头须包含「题目」或 question 列")

    rows = list(reader)
    return _build_items_from_mapped_rows(rows, field_map, col_by_idx=False, start_line=2)


def parse_upload_content(content: bytes, filename: str) -> List[Dict[str, Any]]:
    fmt = detect_upload_format(filename)
    if fmt == "json":
        return parse_json_upload(content)
    if fmt == "xlsx":
        return parse_xlsx_upload(content)
    return parse_csv_upload(content)


def parse_xlsx_upload(content: bytes) -> List[Dict[str, Any]]:
    """解析普通评测集 XLSX 文件（不包含场景子步骤列）。"""
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise DatasetUploadFormatError(f"无法解析 XLSX 文件：{exc}") from exc

    ws = wb.active
    all_rows: List[List[str]] = []
    for row in ws.iter_rows(values_only=True):
        all_rows.append([str(cell) if cell is not None else "" for cell in row])
    wb.close()

    if len(all_rows) < 2:
        raise DatasetUploadFormatError("XLSX 文件至少需要表头行 + 数据行")

    field_map: Dict[int, str] = {}
    for col_idx, raw in enumerate(all_rows[0]):
        canonical = _normalize_header(raw)
        if canonical:
            field_map[col_idx] = canonical

    if "question" not in field_map.values():
        raise DatasetUploadFormatError("XLSX 表头须包含「题目」或 question 列")

    return _build_items_from_mapped_rows(all_rows[1:], field_map, col_by_idx=True, start_line=2)


def validate_dataset_items(
    data: List[Dict[str, Any]],
    *,
    row_label: str = "Item",
) -> List[str]:
    errors: List[str] = []
    for idx, item in enumerate(data):
        try:
            DatasetItem(**item)
        except Exception as exc:
            if row_label == "行":
                errors.append(f"第 {idx + 2} 行: {exc}")
            else:
                errors.append(f"Item {idx}: {exc}")
    return errors


def validate_dataset_items_with_format(
    data: List[Any],
    filename: str,
) -> List[str]:
    if detect_upload_format(filename) == "csv":
        return validate_dataset_items(data, row_label="行")
    return validate_dataset_items(data, row_label="Item")


# ---------------------------------------------------------------------------
# 评测集质量预校验（仅针对无子步骤的普通导入，不阻断，仅返回 warnings）
# ---------------------------------------------------------------------------

# 步骤标题正则（与 scenario_model_answer_draft_service._STEP_LINE_PATTERNS 对齐）
_QUALITY_STEP_PATTERNS = [
    re.compile(r"^步骤\s*\d+\s*[:：]?", re.M),
    re.compile(r"^第\s*[一二三四五六七八九十百]+\s*步", re.M),
    re.compile(r"^SP\d+\s*[:：]?", re.I | re.M),
    re.compile(r"^子问题\s*\d+", re.M),
    re.compile(r"^\d+[、．.]\s+", re.M),
]
# Markdown 标题正则（与 scenario_gt_draft_service._HEADING_RE 对齐）
_QUALITY_MD_HEADING = re.compile(
    r"^(?:#{1,4}\s+\S|(?:(?:\d+\.)+\d*\s+)|(?:第[一二三四五六七八九十\d]+[章节步]\s*))",
    re.M,
)
# #### 步骤N 前缀检测
_QUALITY_MARKDOWN_PREFIX_STEP = re.compile(r"^####\s*步骤\s*\d+", re.M)
# 纯数值关键词（用于 numeric_keyword_heavy 判定）
_QUALITY_NUMERIC_KEYWORD = re.compile(r"^-?\d+(\.\d+)?$")


def _detect_step_format_types(text: str) -> set:
    """检测文本中命中的步骤格式类型集合。"""
    hits = set()
    for i, pat in enumerate(_QUALITY_STEP_PATTERNS):
        if pat.search(text):
            # 0-3 对应：步骤N / 第N步 / SPN / 子问题N / 数字列表
            names = ["step_n", "cn_step", "sp", "sub_q", "num_list"]
            hits.add(names[i])
    return hits


def validate_dataset_quality(
    data: List[Dict[str, Any]],
    *,
    row_label: str = "Item",
) -> List[str]:
    """对导入的评测集做结构质量预校验，返回 warning 列表（不阻断导入）。

    仅适用于无子步骤的普通导入路径；场景 CSV 自带子步骤，不走此校验。
    """
    warnings: List[str] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        # 行号展示：CSV 用"第 N 行"，JSON 用"Item N"
        if row_label == "行":
            label = f"第 {idx + 2} 行"
        else:
            label = f"Item {idx}"

        model_answer = (item.get("model_answer") or "").strip()
        ground_truth = (item.get("ground_truth") or "").strip()

        # 1) no_answer：两者都空（error 级，但这里仍作为 warning 返回，由前端决定是否阻断）
        if not model_answer and not ground_truth:
            warnings.append(f"{label}: 无 model_answer/ground_truth，无法提炼场景子步骤")
            continue

        # 2) model_answer_empty：model_answer 为空时建议填写
        if not model_answer:
            warnings.append(
                f"{label}: model_answer 为空，建议填写包含推理过程的模型答案，"
                "否则场景子步骤无法自动提炼"
            )
            continue  # model_answer 为空，后续结构检测无意义

        # 3) no_step_structure：未识别多步结构
        # 提炼器的判定是"命中行数≥2"（同一格式命中多次也算多步），而非"格式类型数≥2"
        step_hit_count = 0
        all_step_lines = set()
        for pat in _QUALITY_STEP_PATTERNS:
            for m in pat.finditer(model_answer):
                all_step_lines.add(m.start())
        step_hit_count = len(all_step_lines)
        has_md_heading = bool(_QUALITY_MD_HEADING.search(model_answer))
        if step_hit_count < 2 and not has_md_heading:
            warnings.append(
                f"{label}: model_answer 未识别多步结构，自动提炼将生成单步兜底"
            )

        # 4) markdown_prefix_step：#### 步骤N 前缀
        if _QUALITY_MARKDOWN_PREFIX_STEP.search(model_answer):
            warnings.append(
                f"{label}: 步骤标题带 #### 前缀，建议去掉以提升提炼精度"
            )

        # 5) mixed_step_formats：步骤格式混用（命中≥2 种不同格式类型）
        step_hits = _detect_step_format_types(model_answer)
        if len(step_hits) >= 2:
            warnings.append(
                f"{label}: 步骤格式混用（{', '.join(sorted(step_hits))}），"
                "可能导致提炼粒度不均"
            )

        # 6) too_many_steps：步骤数 > 12
        if step_hit_count > 12:
            warnings.append(
                f"{label}: 步骤数超过 12（{len(all_step_lines)}），"
                "自动提炼将被截断，建议拆分题目"
            )

        # 7) numeric_keyword_heavy：数值关键词占比 > 40%
        # 复用关键词抽取逻辑（内联简化版，避免循环依赖）
        kws = _extract_keywords_preview(model_answer)
        if kws:
            numeric_count = sum(
                1 for kw in kws if _QUALITY_NUMERIC_KEYWORD.match(kw)
            )
            ratio = numeric_count / len(kws)
            if ratio > 0.4:
                warnings.append(
                    f"{label}: 数值类关键词占比 {ratio:.0%}，"
                    "建议这些步骤改用基于Prompt评测"
                )

    return warnings


def _extract_keywords_preview(text: str) -> List[str]:
    """关键词抽取预览（简化版，与 scenario_gt_draft_service 对齐）。

    避免循环导入，这里内联一份精简逻辑。
    """
    inline_re = re.compile(
        r"`([^`]+)`|「([^」]+)」|【([^】]+)】|(\b[A-Z][A-Z0-9_]{2,}\b)"
    )
    lines = text.splitlines()
    keywords: List[str] = []
    for line in lines:
        for g in inline_re.findall(line):
            for part in g:
                if part and part.strip():
                    keywords.append(part.strip())
        # 短句兜底
        compact = re.sub(r"\s+", "", line)
        if 2 <= len(compact) <= 24:
            keywords.append(compact[:24])
    # 去重保序
    seen: set = set()
    out: List[str] = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            out.append(kw)
    return out[:12]


EXPORT_INTERNAL_KEYS = {"question_key", "dataset_item_id"}

EXPORT_REDUNDANT_KEYS = {
    "scenario": "scenario_key",
}


def _clean_item_for_export(item: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = dict(item)
    for key in EXPORT_INTERNAL_KEYS:
        cleaned.pop(key, None)
    for dup_key, canonical_key in EXPORT_REDUNDANT_KEYS.items():
        if dup_key in cleaned and canonical_key in cleaned:
            cleaned.pop(dup_key, None)
    return cleaned


def export_dataset_json_bytes(items: List[Dict[str, Any]]) -> bytes:
    cleaned = [_clean_item_for_export(it) for it in items if isinstance(it, dict)]
    return json.dumps(cleaned, ensure_ascii=False, indent=2).encode("utf-8")


def _item_to_export_row(item: Dict[str, Any]) -> Dict[str, str]:
    row: Dict[str, str] = {}
    for header, field in zip(EXPORT_CSV_HEADERS, EXPORT_FIELD_KEYS):
        val = item.get(field)
        if field == "id" and not val:
            val = item.get("dataset_item_id")
        row[header] = "" if val is None else str(val)
    return row


# 场景子步骤导出列（与导入模板 SCENARIO_CSV_HEADERS 的 Step_* 子集一致）
SCENARIO_STEP_EXPORT_HEADERS: List[str] = [
    "Step_Question",
    "Step_Prompt",
    "Step_Ground_Truth",
    "Step_Mode_Answer",
    "Step_Weight",
    "Step_Judging_Mode",
]


def _dataset_has_steps(items: List[Dict[str, Any]]) -> bool:
    return any(
        isinstance(it, dict) and it.get("steps") for it in items
    )


def _step_to_export_fields(step: Dict[str, Any]) -> Dict[str, str]:
    """ScenarioEvalStep dict → Step_* 导出列（导入的逆映射）。

    逆映射规则（与 _do_scenario_import 对齐）：
      - step.name → Step_Question
      - match_rules.eval_config.aggregate_prompt → Step_Prompt（仅 llm_aggregate）
      - gt_expected.text / keywords → Step_Ground_Truth
      - gt_expected.model_answer → Step_Mode_Answer
      - step.weight → Step_Weight
      - eval_config.mode == "llm_aggregate" → Step_Judging_Mode = "基于Prompt评测"，否则 "Ground_Truth评测"
    """
    match_rules = step.get("match_rules")
    eval_config = (
        match_rules.get("eval_config") if isinstance(match_rules, dict) else None
    )
    eval_config = eval_config if isinstance(eval_config, dict) else {}
    mode = eval_config.get("mode")

    step_prompt = ""
    if mode == "llm_aggregate":
        ap = eval_config.get("aggregate_prompt")
        if ap:
            step_prompt = str(ap)

    gt_expected = step.get("gt_expected")
    gt_expected = gt_expected if isinstance(gt_expected, dict) else {}
    gt_text = gt_expected.get("text")
    if not gt_text:
        kws = gt_expected.get("keywords") or []
        if isinstance(kws, list) and kws:
            gt_text = ",".join(str(k) for k in kws)
    step_ground_truth = str(gt_text) if gt_text else ""

    step_model_answer = ""
    ma = gt_expected.get("model_answer")
    if ma:
        step_model_answer = str(ma)

    weight = step.get("weight")
    step_weight = "" if weight is None else str(weight)

    step_judging_mode = "基于Prompt评测" if mode == "llm_aggregate" else "Ground_Truth评测"

    return {
        "Step_Question": str(step.get("name") or ""),
        "Step_Prompt": step_prompt,
        "Step_Ground_Truth": step_ground_truth,
        "Step_Mode_Answer": step_model_answer,
        "Step_Weight": step_weight,
        "Step_Judging_Mode": step_judging_mode,
    }


def _scenario_export_headers() -> List[str]:
    return EXPORT_CSV_HEADERS + SCENARIO_STEP_EXPORT_HEADERS


def export_dataset_csv_bytes(items: List[Dict[str, Any]]) -> bytes:
    has_steps = _dataset_has_steps(items)
    headers = _scenario_export_headers() if has_steps else EXPORT_CSV_HEADERS
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, lineterminator="\n")
    writer.writeheader()
    for item in items:
        if not isinstance(item, dict):
            continue
        base_row = _item_to_export_row(item)
        steps = item.get("steps") if has_steps else None
        if steps:
            for idx, step in enumerate(steps):
                row = dict(base_row) if idx == 0 else {}
                row.update(_step_to_export_fields(step))
                writer.writerow(row)
        else:
            writer.writerow(base_row)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def export_dataset_xlsx_bytes(items: List[Dict[str, Any]]) -> bytes:
    """导出评测集为 XLSX 格式（表头与 CSV 导出一致，英文）。

    含子步骤时追加 Step_* 列，多行格式与 CSV 导出一致。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    has_steps = _dataset_has_steps(items)
    headers = _scenario_export_headers() if has_steps else EXPORT_CSV_HEADERS

    wb = Workbook()
    ws = wb.active
    ws.title = "评测集"

    # 样式
    header_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )
    wrap_align = Alignment(horizontal="left", vertical="top", wrap_text=True)

    # 表头
    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = Font(name="微软雅黑", size=10, bold=True)
        cell.fill = header_fill
        cell.border = thin_border
        cell.alignment = wrap_align

    # 数据行
    row_idx = 2
    for item in items:
        if not isinstance(item, dict):
            continue
        base_row = _item_to_export_row(item)
        steps = item.get("steps") if has_steps else None
        if steps:
            for s_idx, step in enumerate(steps):
                row = dict(base_row) if s_idx == 0 else {}
                row.update(_step_to_export_fields(step))
                for col_idx, header in enumerate(headers, 1):
                    cell = ws.cell(row=row_idx, column=col_idx, value=row.get(header, ""))
                    cell.alignment = wrap_align
                    cell.border = thin_border
                row_idx += 1
        else:
            for col_idx, header in enumerate(headers, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=base_row.get(header, ""))
                cell.alignment = wrap_align
                cell.border = thin_border
            row_idx += 1

    # 列宽自适应
    for col_idx, header in enumerate(headers, 1):
        max_len = len(header)
        for r in range(2, row_idx):
            val = ws.cell(row=r, column=col_idx).value
            if val:
                max_len = max(max_len, min(len(str(val)), 60))
        ws.column_dimensions[get_column_letter(col_idx)].width = max_len + 4

    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.close()
    wb.save(buf)
    return buf.getvalue()


# 场景 XLSX 模板文件路径（项目根 template-scenario.xlsx，已通过 docker-compose 挂载）
SCENARIO_XLSX_TEMPLATE_PATHS: List[str] = [
    "/app/data/template-scenario.xlsx",  # 容器内（docker compose volume mount）
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "template-scenario.xlsx")
    ),  # 宿主机直接运行（项目根）
]


def _read_scenario_xlsx_template_file() -> Optional[bytes]:
    """读取项目根的 template-scenario.xlsx 文件作为场景 XLSX 下载模板。"""
    for path in SCENARIO_XLSX_TEMPLATE_PATHS:
        try:
            with open(path, "rb") as f:
                data = f.read()
            if data:
                return data
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return None


def xlsx_scenario_template_bytes() -> bytes:
    """场景 XLSX 模板：直接返回项目根 ``template-scenario.xlsx`` 文件内容。

    若文件不可用，回退到内置示例模板。
    """
    file_bytes = _read_scenario_xlsx_template_file()
    if file_bytes is not None:
        return file_bytes

    # === 回退：内置示例 XLSX 模板 ===
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "场景评测模板"

    # 样式
    header_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    comment_fill = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")
    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )
    wrap_align = Alignment(horizontal="left", vertical="top", wrap_text=True)

    xlsx_headers = [
        "Question", "Prompt", "Ground_Truth", "Mode_Answer", "Judging_Mode",
        "Step_Question", "Step_Prompt", "Step_Ground_Truth", "Step_Mode_Answer",
        "Step_Weight", "Step_Judging_Mode",
    ]

    # 第 1 行：注释行（合并单元格 + 黄色背景）
    comment_parts = SCENARIO_CSV_COMMENT.split(",")
    for col_idx, val in enumerate(comment_parts[:len(xlsx_headers)], 1):
        cell = ws.cell(row=1, column=col_idx, value=val)
        cell.font = Font(name="微软雅黑", size=9, italic=True)
        cell.fill = comment_fill
        cell.border = thin_border

    # 冻结前 2 行
    ws.freeze_panes = "A3"

    # 第 2 行：表头
    for col_idx, val in enumerate(xlsx_headers, 1):
        cell = ws.cell(row=2, column=col_idx, value=val)
        cell.font = Font(name="微软雅黑", size=10, bold=True)
        cell.fill = header_fill
        cell.border = thin_border
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # 示例数据行
    example_rows = [
        ["请对XX产品进行上市前卖点分析", "你是数据质量评测专家，请根据主问题整体回答。", "", "", "Ground_Truth评测",
         "步骤1：客户声音样本有哪些？", "请基于输出对客户声音结构化结果进行评测。", "样本量,数据来源,时间范围", "", "", "Ground_Truth评测"],
        ["", "", "", "", "",
         "步骤2：可以聚类成哪几类客群？", "请基于聚类饼图结果进行评测。", "客群类型,样本占比", "", "", "Ground_Truth评测"],
        ["", "", "", "", "",
         "步骤3：可以识别哪些关键竞品车型？", "## 角色\n你是严格的数据质量评测专家。", "", "示例模型答案", "0.3", "基于Prompt评测"],
        ["", "", "", "", "",
         "步骤4：可以形成哪些待验证卖点？", "## 角色\n请评测匹配度与优劣对比正确率。", "", "示例模型答案", "0.4", "基于Prompt评测"],
    ]

    for row_idx, row_data in enumerate(example_rows, 3):
        for col_idx, val in enumerate(row_data, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.font = Font(name="微软雅黑", size=9)
            cell.border = thin_border
            cell.alignment = wrap_align

    # 列宽
    col_widths = [25, 30, 20, 20, 12, 28, 35, 22, 20, 10, 12]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # 数据行高度
    for r in range(3, 3 + len(example_rows)):
        ws.row_dimensions[r].height = 80

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv_template_bytes() -> bytes:
    example = _item_to_export_row(
        {
            "question": "示例题目",
            "ground_truth": "标准答案",
            "model_answer": "模型答案（离线必填，在线可留空）",
            "context": "",
            "scenario_key": "",
        }
    )
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_CSV_HEADERS, lineterminator="\n")
    writer.writeheader()
    writer.writerow(example)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


# 场景 CSV 表头顺序：主表 5 列 + 步骤子表 6 列
SCENARIO_CSV_HEADERS: List[str] = [
    "Question",
    "Prompt",
    "Ground_Truth",
    "Mode_Answer",
    "Judging_Mode",
    "Step_Question",
    "Step_Prompt",
    "Step_Ground_Truth",
    "Step_Mode_Answer",
    "Step_Weight",
    "Step_Judging_Mode",
]

# 注释行（首行说明各列含义）
SCENARIO_CSV_COMMENT: str = (
    "说明：该行为固定值，勿动！,基于Prompt评测需要填写,Ground_Truth评测需要，基于Prompt评测可填可不填,"
    "离线评测答案,评测模式,"
    "过程问题（不填则系统从 Model_Answer 自动提炼子步骤，精度可能打折，建议填写）,"
    "提示词评审需要填写，基于Prompt评测不填,"
    "Ground_Truth评测需要填写，基于Prompt评测可填可不填,离线评测答案,"
    "每个步骤的权重，在0~1之间，加起来等于1,过程评测模式"
)


# 场景 CSV 模板文件路径（项目根 template-scenario.csv，已通过 docker-compose 挂载）
SCENARIO_CSV_TEMPLATE_PATHS: List[str] = [
    "/app/data/template-scenario.csv",  # 容器内（docker compose volume mount）
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "template-scenario.csv")
    ),  # 宿主机直接运行（项目根）
]


def _read_scenario_csv_template_file() -> Optional[bytes]:
    """读取项目根的 template-scenario.csv 文件作为场景 CSV 下载模板。

    优先使用挂载路径，读取失败时回退到内置生成的模板。
    返回 None 表示内置模板回退。
    """
    for path in SCENARIO_CSV_TEMPLATE_PATHS:
        try:
            with open(path, "rb") as f:
                data = f.read()
            if not data:
                continue
            # 统一加 UTF-8 BOM，避免 Excel 打开中文乱码
            if not data.startswith(b"\xef\xbb\xbf"):
                data = b"\xef\xbb\xbf" + data
            return data
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return None


def csv_scenario_template_bytes() -> bytes:
    """场景 CSV 模板：直接返回项目根 ``template-scenario.csv`` 文件内容。

    若文件不可用（例如本地开发环境未挂载），回退到内置示例模板。
    """
    file_bytes = _read_scenario_csv_template_file()
    if file_bytes is not None:
        return file_bytes

    # === 回退：内置示例模板 ===
    rows: List[List[str]] = [
        # 主问题行 + 步骤 1（Ground_Truth评测）
        [
            "示例题目：请对XX产品进行上市前卖点分析",
            "你是数据质量评测专家，请根据主问题整体回答。",
            "",
            "",
            "Ground_Truth评测",
            "步骤1：客户声音样本有哪些？",
            "请基于输出对客户声音结构化结果进行评测。",
            "样本量,数据来源,时间范围",
            "",
            "",
            "Ground_Truth评测",
        ],
        # 主问题字段留空 + 步骤 2（Ground_Truth评测）
        [
            "", "", "", "", "",
            "步骤2：可以聚类成哪几类客群？",
            "请基于聚类饼图结果进行评测。",
            "客群类型,样本占比",
            "",
            "",
            "Ground_Truth评测",
        ],
        # 主问题字段留空 + 步骤 3（基于Prompt评测）
        [
            "", "", "", "", "",
            "步骤3：可以识别哪些关键竞品车型？",
            (
                "## 角色\n你是严格的数据质量评测专家，请对竞品定位步骤进行评测。\n\n"
                "## 任务1：图表结构\n标准：\n- 图表类型为 bubble\n"
                "- 包含 x_axis_name / y_axis_name / filter_bounds\n- TOP5 竞品 5 个\n\n"
                "## 输出\n```json\n{\n  \"activity\": \"活动4-竞品定位\",\n"
                "  \"overall_pass\": true\n}\n```"
            ),
            "",
            "示例模型答案：竞品包含 A/B/C/D/E 等 5 个车型。",
            "0.3",
            "基于Prompt评测",
        ],
        # 主问题字段留空 + 步骤 4（基于Prompt评测）
        [
            "", "", "", "", "",
            "步骤4：可以形成哪些待验证卖点？",
            (
                "## 角色\n请评测匹配度与优劣对比正确率（≥80%）。\n\n"
                "## 输出\n```json\n{ \"accuracy\": \"86.9%\", \"pass\": true }\n```"
            ),
            "",
            "示例模型答案：匹配度评估结果……",
            "0.4",
            "基于Prompt评测",
        ],
    ]

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    # 注释行
    writer.writerow(SCENARIO_CSV_COMMENT.split(","))
    # 表头行
    writer.writerow(SCENARIO_CSV_HEADERS)
    # 数据行
    for r in rows:
        writer.writerow(r)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def safe_export_filename(dataset_name: str, ext: str) -> str:
    safe = "".join(
        c
        if c.isascii() and (c.isalnum() or c in "._-")
        else "_"
        for c in (dataset_name or "dataset")
    )
    safe = re.sub(r"_+", "_", safe)
    safe = safe.strip("_") or "dataset"
    safe = safe.strip(".") or "dataset"
    return f"{safe}.{ext}"


def content_disposition_attachment(filename: str) -> str:
    """ASCII fallback + RFC 5987 filename* for non Latin-1 names."""
    from urllib.parse import quote

    if "." in filename:
        base, ext = filename.rsplit(".", 1)
    else:
        base, ext = filename, "dat"
    ascii_fallback = safe_export_filename(base, ext)
    encoded = quote(filename, safe="")
    return f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'


# ── 扩展 CSV 解析（支持场景子步骤逐行导入） ──────────────────────────────

SCENARIO_STEP_HEADERS = [
    "Step_Question",
    "Step_Prompt",
    "Step_Ground_Truth",
    "Step_Judging_Mode",
]

SCENARIO_STEP_HEADER_MAP = {
    "Step_Question": "step_question",
    "step_question": "step_question",
    "步骤问题": "step_question",
    "Step_Prompt": "step_prompt",
    "step_prompt": "step_prompt",
    "步骤评测提示词": "step_prompt",
    "Step_Ground_Truth": "step_ground_truth",
    "step_ground_truth": "step_ground_truth",
    "步骤标准答案": "step_ground_truth",
    "Step_Judging_Mode": "step_judging_mode",
    "step_judging_mode": "step_judging_mode",
    "步骤评测方式": "step_judging_mode",
    "Judging_Mode": "judging_mode",
    "judging_mode": "judging_mode",
    "评测方式": "judging_mode",
    "Mode_Answer": "model_answer",
    "Step_Mode_Answer": "step_model_answer",
    "step_model_answer": "step_model_answer",
    "Step_Weight": "step_weight",
    "step_weight": "step_weight",
}


def _normalize_extended_header(header: str) -> Optional[str]:
    """扩展表头映射：支持常规字段 + Step_* 步骤字段。

    注意：Prompt 列在场景CSV中是评测提示词而非题目，因此不映射到 question。
    """
    key = (header or "").strip()
    if not key:
        return None
    # 先查步骤字段
    step_mapped = SCENARIO_STEP_HEADER_MAP.get(key) or SCENARIO_STEP_HEADER_MAP.get(key.lower())
    if step_mapped:
        return step_mapped
    # Prompt 在场景CSV中不映射到 question
    lower = key.lower()
    if lower == "prompt":
        return "prompt"
    # 再查标准字段（排除 prompt→question 的歧义映射）
    std = _normalize_header(key)
    if std and std != "question":
        return std
    if lower == "question" or key == "题目":
        return "question"
    return None


def is_scenario_csv(headers: List[str]) -> bool:
    """判断 CSV 是否包含场景子步骤列（Step_Question 等）。"""
    normalized = {_normalize_extended_header(h) for h in headers}
    step_keys = {"step_question", "step_prompt", "step_ground_truth", "step_judging_mode"}
    return bool(normalized & step_keys)


def parse_scenario_csv(content: bytes) -> Dict[str, Any]:
    """解析「问题 + 步骤」格式的扩展 CSV，返回 question_items + steps 结构。"""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DatasetUploadFormatError("CSV 须为 UTF-8 编码") from exc

    # 预处理：跳过首行注释（如 "说明：该行为固定值，勿动！"）
    lines = text.splitlines()
    if lines and lines[0].startswith("说明"):
        lines = lines[1:]
    clean_text = "\n".join(lines)

    reader = csv.DictReader(io.StringIO(clean_text))
    if not reader.fieldnames:
        raise DatasetUploadFormatError("CSV 缺少表头行")

    field_map: Dict[str, str] = {}
    for raw in reader.fieldnames:
        canonical = _normalize_extended_header(raw)
        if canonical:
            field_map[raw] = canonical

    if "question" not in field_map.values():
        raise DatasetUploadFormatError("CSV 表头须包含「题目」或 question 列")

    rows = list(reader)
    return _build_scenario_from_mapped_rows(rows, field_map, col_by_idx=False, start_line=2, format_label="CSV")


def parse_scenario_xlsx(content: bytes) -> Dict[str, Any]:
    """解析「问题 + 步骤」格式的 XLSX 文件，返回与 ``parse_scenario_csv`` 相同结构。

    XLSX 模板格式：
    - 第 1 行：注释说明行（自动跳过）
    - 第 2 行：表头
    - 第 3+ 行：数据（合并单元格按向下填充处理）
    """
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise DatasetUploadFormatError(f"无法解析 XLSX 文件：{exc}") from exc

    ws = wb.active

    # 读取所有行
    all_rows: List[List[str]] = []
    for row in ws.iter_rows(values_only=True):
        all_rows.append([str(cell) if cell is not None else "" for cell in row])
    wb.close()

    if len(all_rows) < 2:
        raise DatasetUploadFormatError("XLSX 文件至少需要表头行 + 数据行")

    # 跳过首行注释
    start_row = 0
    if all_rows[0] and all_rows[0][0].startswith("说明"):
        start_row = 1

    header_row = all_rows[start_row]
    data_rows = all_rows[start_row + 1:]

    field_map: Dict[int, str] = {}
    for col_idx, raw in enumerate(header_row):
        canonical = _normalize_extended_header(raw)
        if canonical:
            field_map[col_idx] = canonical

    if "question" not in field_map.values():
        raise DatasetUploadFormatError("XLSX 表头须包含「题目」或 Question 列")

    # 预处理：向下填充合并单元格中为空的 question/prompt/ground_truth 等字段
    last_q_vals: Dict[str, str] = {}
    processed_rows: List[List[str]] = []
    for row in data_rows:
        if not row or not any(v.strip() for v in row):
            processed_rows.append(row)
            continue

        # 扩展行到与表头对齐
        if len(row) < len(header_row):
            row = list(row) + [""] * (len(header_row) - len(row))

        for col_idx, value in enumerate(row):
            canonical = field_map.get(col_idx)
            if (canonical in ("question", "prompt", "ground_truth", "model_answer", "judging_mode")
                    and not (value or "").strip()):
                row[col_idx] = last_q_vals.get(canonical, "")

        # 更新 last_q_vals
        for col_idx, value in enumerate(row):
            canonical = field_map.get(col_idx)
            if canonical in ("question", "prompt", "ground_truth", "model_answer", "judging_mode"):
                val = (value or "").strip()
                if val:
                    last_q_vals[canonical] = val
        processed_rows.append(row)

    return _build_scenario_from_mapped_rows(
        processed_rows, field_map, col_by_idx=True,
        start_line=start_row + 2, format_label="XLSX")




