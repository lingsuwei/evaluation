"""评测报告导出 demo：用 mock 数据生成完整 5-sheet xlsx，配色对齐 web CSS 变量。

运行: python scripts/demo_export_report.py
输出: scripts/demo_eval_report.xlsx
"""
from __future__ import annotations

import os
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ─── 配色（对齐 frontend/src/index.css 的 CSS 变量） ───
C_PRIMARY = "6B5CE7"        # --edp-primary 紫
C_PRIMARY_SOFT = "EEE9FF"   # --edp-primary-soft
C_GREEN = "22A06B"          # --edp-green
C_GREEN_SOFT = "E3F6EC"     # --edp-green-soft
C_AMBER = "E6A817"          # --edp-amber
C_AMBER_SOFT = "FDF3D8"     # --edp-amber-soft
C_RED = "DC3545"            # --edp-red
C_RED_SOFT = "FDE2E4"       # --edp-red-soft
C_TEXT3 = "9099B0"          # --edp-text3 灰
C_BG_ZEBRA = "F7F9FC"       # 数据行斑马
C_BORDER = "D0D5DD"         # 浅灰边框
C_WHITE = "FFFFFF"

# ─── 字体 ───
F_HEADER = Font(name="微软雅黑", bold=True, color=C_WHITE, size=11)
F_TITLE = Font(name="微软雅黑", bold=True, color=C_PRIMARY, size=18)
F_SUBTITLE = Font(name="微软雅黑", color=C_TEXT3, size=10)
F_SECTION = Font(name="微软雅黑", bold=True, color=C_PRIMARY, size=12)
F_KPI_VAL = Font(name="Arial", bold=True, color=C_PRIMARY, size=20)
F_KPI_LBL = Font(name="微软雅黑", color=C_TEXT3, size=10)
F_DATA = Font(name="Arial", size=10)
F_DATA_BOLD = Font(name="Arial", size=10, bold=True)
F_FOOTNOTE = Font(name="Arial", color=C_TEXT3, size=9)

# ─── 填充 ───
FILL_HEADER = PatternFill(start_color=C_PRIMARY, end_color=C_PRIMARY, fill_type="solid")
FILL_GREEN = PatternFill(start_color=C_GREEN_SOFT, end_color=C_GREEN_SOFT, fill_type="solid")
FILL_AMBER = PatternFill(start_color=C_AMBER_SOFT, end_color=C_AMBER_SOFT, fill_type="solid")
FILL_RED = PatternFill(start_color=C_RED_SOFT, end_color=C_RED_SOFT, fill_type="solid")
FILL_GRAY = PatternFill(start_color="F0F2F5", end_color="F0F2F5", fill_type="solid")
FILL_ZEBRA = PatternFill(start_color=C_BG_ZEBRA, end_color=C_BG_ZEBRA, fill_type="solid")
FILL_WHITE = PatternFill(start_color=C_WHITE, end_color=C_WHITE, fill_type="solid")
FILL_PRIMARY_SOFT = PatternFill(start_color=C_PRIMARY_SOFT, end_color=C_PRIMARY_SOFT, fill_type="solid")

# ─── 边框 ───
BORDER_THIN = Border(
    left=Side(style="thin", color=C_BORDER),
    right=Side(style="thin", color=C_BORDER),
    top=Side(style="thin", color=C_BORDER),
    bottom=Side(style="thin", color=C_BORDER),
)

A_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
A_LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)
A_LEFT_TOP = Alignment(horizontal="left", vertical="top", wrap_text=True)


def _status_fill_font(status: str):
    """verdict status → (fill, font color)。"""
    mapping = {
        "pass": (FILL_GREEN, C_GREEN),
        "warn": (FILL_AMBER, C_AMBER),
        "fail": (FILL_RED, C_RED),
        "na": (FILL_GRAY, C_TEXT3),
        "disabled": (FILL_GRAY, C_TEXT3),
        "descriptive": (FILL_PRIMARY_SOFT, C_PRIMARY),
    }
    return mapping.get(status, (FILL_GRAY, C_TEXT3))


def _status_label(status: str) -> str:
    return {"pass": "达标", "warn": "需关注", "fail": "未达标",
            "na": "不适用", "disabled": "未启用", "descriptive": "描述性"}.get(status, status)


# ═══════════════════════════════════════════════════════════
# Mock 数据（模拟一个 301 题的评估任务，4 维全启用，3 轮推理）
# ═══════════════════════════════════════════════════════════
TASK_META = {
    "task_id": "task_685f99eb2bba45f490b02214ccdfc4a0",
    "dataset_name": "300-2评测集",
    "agent_name": "家电助手0805-02",
    "agent_version": "v1.3.0",
    "eval_model": "deepseek-v4-pro",
    "temperature": 0.0,
    "eval_time": "2026-08-11",
    "created_at": "2026-08-11 14:32:01",
    "completed_at": "2026-08-11 15:18:44",
    "evaluation_mode": "在线评估",
    "inference_rounds": 3,
}

# 质量分（直接来自 quality_score 读模型）
QUALITY_SCORE = {
    "total_score": 0.872,
    "result_score": 0.928,
    "process_score": 0.810,
    "stability_score": 0.765,
    "metric_breakdown": {"correctness": 0.887, "faithfulness": 0.91, "answer_relevancy": 0.95},
    "gates": [
        {"type": "low_result_score", "applied": False, "cap": 0.55, "reason": "结果分 ≥ 0.5"},
    ],
}

# 四维结论（直接来自 dimension_verdicts 读模型）
DIM_VERDICTS = {
    "quality": {
        "status": "pass",
        "score": 0.872,
        "headline": "质量总分 0.872，结果分 0.928",
        "metrics": {"result_score": 0.928, "pass_rate": 88.7},
    },
    "process_consistency": {
        "status": "warn",
        "score": 0.810,
        "headline": "3 题存在链条不一致；必达点通过率 81%",
        "metrics": {"green": 268, "yellow": 22, "red": 11, "chain_pass_rate": 89.0,
                    "multi_run_questions": 11, "weakest_chain": "q180"},
    },
    "scenario_delivery": {
        "status": "pass",
        "score": 0.843,
        "headline": "场景综合 0.843；必做步骤 28/30 通过；最弱 步骤3 均分 0.71",
        "metrics": {"process_score": 0.843, "required_pass_rate": 93.3,
                    "required_passed": 28, "required_total": 30,
                    "question_count": 301, "volatile_question_count": 3,
                    "weakest_step": {"name": "步骤3:库存校验", "avg_score": 0.71}},
    },
    "runtime_stability": {
        "status": "descriptive",
        "headline": "多轮稳定性：性能偏高",
        "structural": {
            "status": "warn",
            "headline": "5 题存在 step 拓扑波动",
            "metrics": {"multi_round_question_count": 301, "drift_question_count": 5,
                        "drift_question_ratio": 1.7},
        },
        "performance": {
            "status": "warn",
            "headline": "延迟通过率 74%，Token 通过率 82%",
            "metrics": {"latency_pass_rate": 74.0, "tokens_pass_rate": 82.0,
                        "avg_latency_ms": 4250, "target_latency_ms": 3500,
                        "avg_tokens": 1820, "target_tokens": 2000},
        },
        "quality_stability": {
            "status": "pass",
            "score": 0.81,
            "metrics": {"correctness_pass_rate": 87.4, "std": 0.092},
        },
    },
}

# 指标汇总（直接来自 metrics_summary）
METRICS_SUMMARY = [
    {"metric_name": "correctness", "avg_score": 0.887, "pass_rate": 88.7,
     "passed_count": 267, "total_count": 301, "scored_count": 301, "threshold": 0.80,
     "eval_method": "llm"},
    {"metric_name": "faithfulness", "avg_score": 0.910, "pass_rate": 91.0,
     "passed_count": 274, "total_count": 301, "scored_count": 301, "threshold": 0.80,
     "eval_method": "llm"},
    {"metric_name": "answer_relevancy", "avg_score": 0.950, "pass_rate": 95.0,
     "passed_count": 286, "total_count": 301, "scored_count": 301, "threshold": 0.80,
     "eval_method": "llm"},
]

# 指标展示名映射（与 web 端 metricLabelMap 保持一致）
METRIC_CN = {
    # 质量指标
    "correctness": "正确性",
    "faithfulness": "忠实度(幻觉)",
    "answer_relevancy": "答案相关性",
    # 安全指标
    "role_violation": "角色越界",
    "bias": "偏见",
    "toxicity": "毒性",
    "pii_leakage": "PII泄露",
    "non_advice": "非建议",
    "misuse": "误用",
    # 工具/推理指标
    "task_completion": "任务完成度",
    "argument_correctness": "参数正确性",
    "step_efficiency": "步骤效率",
    "plan_adherence": "计划遵循",
    "plan_quality": "计划质量",
    # RAG 指标
    "contextual_relevancy": "检索相关性",
    "contextual_precision": "检索精确度",
    "contextual_recall": "检索召回率",
    # 多轮指标
    "conversation_completeness": "多轮完整性",
    "knowledge_retention": "知识留存",
    "role_adherence": "角色遵循",
    "turn_relevancy": "多轮相关性",
    "goal_accuracy": "目标达成",
    # 运行效能
    "runtime.inference_latency_ms": "推理耗时(ms)",
    "runtime.tokens_total": "Token消耗",
}

# 题目详情 mock（含多轮推理）
QUESTIONS = [
    {"qid": 30, "category": "履约调拨与供货分配", "score": 0.0, "run_index": 0,
     "question": "如果OL-618-0003（AC-35-A，6台，允许拆单）的候选来源仓为SWH-2-1和SWH-4-1，拆单分配结果是什么？",
     "agent_answer": "[推理失败: 推理接口调用超时]",
     "ground_truth": "核心结论：允许拆单时，6台AC-35-A全部分配给SWH-2-1（本地库存直接履约），无需从SWH-4-1调拨。",
     "eval_reason": "回答仅显示推理失败，未提供任何有效信息，完全未涉及标准答案中的核心结论。",
     "passed": False,
     "metric_scores": {"correctness": 0.0, "faithfulness": 0.0, "answer_relevancy": 0.0, "runtime.inference_latency_ms": 30012, "runtime.tokens_total": 0},
     "metric_passed": {"correctness": False, "faithfulness": False, "answer_relevancy": False, "runtime.inference_latency_ms": False, "runtime.tokens_total": True},
     "metric_reasons": {"correctness": "回答仅显示推理失败，未提供任何有效信息。", "faithfulness": "无有效回答，无法评估忠实度。", "answer_relevancy": "推理接口超时，无回答内容。"},
     "rounds": [
         {"round_num": 1, "score": 0.0, "duration_ms": 30012, "answer": "[推理失败: 推理接口调用超时]"},
         {"round_num": 2, "score": 0.0, "duration_ms": 28950, "answer": "[推理失败: 推理接口调用超时]"},
         {"round_num": 3, "score": 0.0, "duration_ms": 30100, "answer": "[推理失败: 推理接口调用超时]"},
     ]},
    {"qid": 41, "category": "优先级计算", "score": 0.0, "run_index": 0,
     "question": "执行ORD-618-0001的订单优先级计算，请列出脚本所需参数。",
     "agent_answer": "订单优先级计算已完成。以下是 ORD-618-0001 两条订单明细的优先级评分结果...",
     "ground_truth": "核心结论：订单优先级计算脚本需要orders_json（每项含priority_result_id、campaign_id、order_line_id等10个字段）。",
     "eval_reason": "回答完全错误，与问题要求无关。问题要求列出脚本所需参数，回答却给出了计算结果。",
     "passed": False,
     "metric_scores": {"correctness": 0.0, "faithfulness": 0.1, "answer_relevancy": 0.05, "runtime.inference_latency_ms": 5230, "runtime.tokens_total": 850},
     "metric_passed": {"correctness": False, "faithfulness": False, "answer_relevancy": False, "runtime.inference_latency_ms": True, "runtime.tokens_total": True},
     "metric_reasons": {"correctness": "回答给出计算结果而非所需参数，完全偏离问题。", "faithfulness": "回答内容缺乏依据。", "answer_relevancy": "未回答脚本参数问题。"},
     "rounds": [
         {"round_num": 1, "score": 0.0, "duration_ms": 5230, "answer": "订单优先级计算已完成..."},
         {"round_num": 2, "score": 0.1, "duration_ms": 5410, "answer": "需要的参数包括订单ID..."},
         {"round_num": 3, "score": 0.0, "duration_ms": 5180, "answer": "订单优先级计算已完成..."},
     ]},
    {"qid": 180, "category": "基础数据查询", "score": 0.0, "run_index": 0,
     "question": "2026-06-01所有仓库的剩余出库能力排名前3的是哪些？",
     "agent_answer": "已获取2026-06-01全部12个仓库的作业能力数据...",
     "ground_truth": "核心结论：剩余出库能力前3为SWH-4-3、SWH-4-2、SWH-4-1。",
     "eval_reason": "回答未提供问题所要求的前3仓库信息，仅列出原始数据。",
     "passed": False,
     "metric_scores": {"correctness": 0.0, "faithfulness": 0.2, "answer_relevancy": 0.1, "runtime.inference_latency_ms": 4120, "runtime.tokens_total": 1200},
     "metric_passed": {"correctness": False, "faithfulness": False, "answer_relevancy": False, "runtime.inference_latency_ms": True, "runtime.tokens_total": True},
     "metric_reasons": {"correctness": "未给出前3仓库排名，仅列出原始数据。", "faithfulness": "数据引用不完整。", "answer_relevancy": "未按问题要求排序输出。"},
     "rounds": [
         {"round_num": 1, "score": 0.0, "duration_ms": 4120, "answer": "已获取2026-06-01全部12个仓库..."},
         {"round_num": 2, "score": 0.0, "duration_ms": 3980, "answer": "查询结果如下..."},
         {"round_num": 3, "score": 0.0, "duration_ms": 4250, "answer": "已获取数据..."},
     ]},
    {"qid": 179, "category": "基础数据查询", "score": 0.2, "run_index": 0,
     "question": "SWH-4-3仓库在2026-06-01的剩余出库能力是多少？",
     "agent_answer": "查询到了SWH-4-3仓库在2026-06-01的作业能力记录...",
     "ground_truth": "核心结论：SWH-4-3剩余出库能力为2170台。",
     "eval_reason": "回答未给出剩余出库能力具体数值，虽列出基础数据但拒绝计算。",
     "passed": False,
     "metric_scores": {"correctness": 0.2, "faithfulness": 0.5, "answer_relevancy": 0.3, "runtime.inference_latency_ms": 2100, "runtime.tokens_total": 600},
     "metric_passed": {"correctness": False, "faithfulness": False, "answer_relevancy": False, "runtime.inference_latency_ms": True, "runtime.tokens_total": True},
     "metric_reasons": {"correctness": "未给出剩余出库能力具体数值。", "faithfulness": "引用了部分正确数据。", "answer_relevancy": "列出数据但未完成计算。"},
     "rounds": [
         {"round_num": 1, "score": 0.2, "duration_ms": 2100, "answer": "查询到了SWH-4-3仓库..."},
         {"round_num": 2, "score": 0.2, "duration_ms": 2050, "answer": "查询到了SWH-4-3仓库..."},
         {"round_num": 3, "score": 0.2, "duration_ms": 2180, "answer": "查询到了SWH-4-3仓库..."},
     ]},
    {"qid": 1, "category": "履约调拨与供货分配", "score": 0.55, "run_index": 0,
     "question": "请完成ORD-618-0001订单的完整履约分析，包括订单详情、行项明细和来源仓筛选。",
     "agent_answer": "订单ORD-618-0001的履约分析如下...",
     "ground_truth": "核心结论：订单含2个行项，来源仓推荐SWH-2-1。",
     "eval_reason": "回答部分正确，缺失来源仓筛选逻辑。",
     "passed": False,
     "metric_scores": {"correctness": 0.55, "faithfulness": 0.7, "answer_relevancy": 0.6, "runtime.inference_latency_ms": 6800, "runtime.tokens_total": 2100},
     "metric_passed": {"correctness": False, "faithfulness": True, "answer_relevancy": True, "runtime.inference_latency_ms": True, "runtime.tokens_total": True},
     "metric_reasons": {"correctness": "缺失来源仓筛选逻辑。", "faithfulness": "回答内容与问题相关。", "answer_relevancy": "覆盖了订单详情和行项明细。"},
     "rounds": [
         {"round_num": 1, "score": 0.55, "duration_ms": 6800, "answer": "订单ORD-618-0001..."},
         {"round_num": 2, "score": 0.60, "duration_ms": 7100, "answer": "订单ORD-618-0001..."},
         {"round_num": 3, "score": 0.55, "duration_ms": 6950, "answer": "订单ORD-618-0001..."},
     ]},
    {"qid": 29, "category": "产品信息", "score": 0.75, "run_index": 0,
     "question": "REF-520-A产品的基本信息是什么？",
     "agent_answer": "REF-520-A是净鲜520升冰箱...",
     "ground_truth": "REF-520-A：净鲜520升冰箱，品牌XX，能效一级。",
     "eval_reason": "回答基本正确，缺失能效信息。",
     "passed": False,
     "metric_scores": {"correctness": 0.75, "faithfulness": 0.85, "answer_relevancy": 0.8, "runtime.inference_latency_ms": 1800, "runtime.tokens_total": 350},
     "metric_passed": {"correctness": True, "faithfulness": True, "answer_relevancy": True, "runtime.inference_latency_ms": True, "runtime.tokens_total": True},
     "metric_reasons": {"correctness": "缺失能效等级信息。", "faithfulness": "信息基本可靠。", "answer_relevancy": "回答了产品基本信息。"},
     "rounds": [
         {"round_num": 1, "score": 0.75, "duration_ms": 1800, "answer": "REF-520-A是净鲜520升冰箱..."},
         {"round_num": 2, "score": 0.80, "duration_ms": 1750, "answer": "REF-520-A是净鲜520升冰箱..."},
         {"round_num": 3, "score": 0.75, "duration_ms": 1820, "answer": "REF-520-A是净鲜520升冰箱..."},
     ]},
    {"qid": 100, "category": "基础数据查询", "score": 0.95, "run_index": 0,
     "question": "STORE-001门店的基本信息是什么？",
     "agent_answer": "门店编号STORE-001，门店名称测试门店001...",
     "ground_truth": "门店编号STORE-001，门店名称测试门店001，区域REG-EAST。",
     "eval_reason": "回答正确，覆盖所有关键信息。",
     "passed": True,
     "metric_scores": {"correctness": 0.95, "faithfulness": 1.0, "answer_relevancy": 0.95, "runtime.inference_latency_ms": 1500, "runtime.tokens_total": 280},
     "metric_passed": {"correctness": True, "faithfulness": True, "answer_relevancy": True, "runtime.inference_latency_ms": True, "runtime.tokens_total": True},
     "metric_reasons": {"correctness": "回答完整正确。", "faithfulness": "完全忠实于数据。", "answer_relevancy": "完全切题。"},
     "rounds": [
         {"round_num": 1, "score": 0.95, "duration_ms": 1500, "answer": "门店编号STORE-001..."},
         {"round_num": 2, "score": 0.95, "duration_ms": 1480, "answer": "门店编号STORE-001..."},
         {"round_num": 3, "score": 0.95, "duration_ms": 1520, "answer": "门店编号STORE-001..."},
     ]},
    {"qid": 101, "category": "库存占用查询", "score": 0.88, "run_index": 0,
     "question": "SWH-1-1仓库中CAMP-618-2026活动有多少条有效库存占用记录？",
     "agent_answer": "CAMP-618-2026活动在SWH-1-1仓库共有24条有效库存占用记录...",
     "ground_truth": "核心结论：SWH-1-1仓库中关联CAMP-618-2026活动的有效库存占用记录共24条。",
     "eval_reason": "回答正确，数量与标准答案一致。",
     "passed": True,
     "metric_scores": {"correctness": 0.88, "faithfulness": 0.92, "answer_relevancy": 0.9, "runtime.inference_latency_ms": 3200, "runtime.tokens_total": 520},
     "metric_passed": {"correctness": True, "faithfulness": True, "answer_relevancy": True, "runtime.inference_latency_ms": True, "runtime.tokens_total": True},
     "metric_reasons": {"correctness": "数量与标准答案一致。", "faithfulness": "数据引用准确。", "answer_relevancy": "直接回答了问题。"},
     "rounds": [
         {"round_num": 1, "score": 0.88, "duration_ms": 3200, "answer": "CAMP-618-2026活动..."},
         {"round_num": 2, "score": 0.90, "duration_ms": 3100, "answer": "CAMP-618-2026活动..."},
         {"round_num": 3, "score": 0.88, "duration_ms": 3250, "answer": "CAMP-618-2026活动..."},
     ]},
]

# 场景子步骤（来自 scenario_delivery 维度）
SCENARIO_STEPS = [
    {"step_key": "step1_info_collect", "name": "步骤1:信息收集", "avg_score": 0.89, "required": False},
    {"step_key": "step2_query", "name": "步骤2:数据查询", "avg_score": 0.85, "required": False},
    {"step_key": "step3_inventory_check", "name": "步骤3:库存校验", "avg_score": 0.71, "required": True},
    {"step_key": "step4_allocate", "name": "步骤4:分配计算", "avg_score": 0.82, "required": True},
    {"step_key": "step5_output", "name": "步骤5:结果输出", "avg_score": 0.91, "required": True},
]


# ═══════════════════════════════════════════════════════════
# Sheet 构建器
# ═══════════════════════════════════════════════════════════

def _set_col_widths(ws, widths: dict):
    for col_letter, w in widths.items():
        ws.column_dimensions[col_letter].width = w


def _apply_border(ws, cell_range: str):
    for row in ws[cell_range]:
        for cell in row:
            cell.border = BORDER_THIN


def build_overview_sheet(wb):
    """Sheet 1: 评估总览。"""
    ws = wb.create_sheet("评估总览")
    _set_col_widths(ws, {"A": 22, "B": 14, "C": 18, "D": 12, "E": 55, "F": 12, "G": 14, "H": 12})

    # 标题
    ws.merge_cells("A1:H1")
    ws["A1"] = f"{TASK_META['dataset_name']} 评测报告"
    ws["A1"].font = F_TITLE
    ws["A1"].alignment = A_LEFT
    ws.row_dimensions[1].height = 36

    # 副标题
    ws.merge_cells("A2:H2")
    ws["A2"] = (f"Onto-Eval 本体评测平台 | Agent: {TASK_META['agent_name']} | "
                f"评测模型: {TASK_META['eval_model']} | 评测时间: {TASK_META['eval_time']}")
    ws["A2"].font = F_SUBTITLE
    ws["A2"].alignment = A_LEFT
    ws.row_dimensions[2].height = 20

    # ── KPI 卡片区 (行4-6) ──
    kpi_cards = [
        ("质量总分", f"{QUALITY_SCORE['total_score']:.3f}", f"{QUALITY_SCORE['total_score']*100:.1f}%", "pass"),
        ("正确性通过率", "88.7%", "267/301题", "pass"),
        ("未通过题数", "34", "34/301题", "fail"),
        ("0分题数", "4", "4/301题", "fail"),
    ]
    # 4 张卡，每卡 2 列，共 8 列
    for i, (label, val, sub, status) in enumerate(kpi_cards):
        col_start = 1 + i * 2
        col_end = col_start + 1
        col_letter_s = get_column_letter(col_start)
        col_letter_e = get_column_letter(col_end)
        # 行4: 标签
        ws.merge_cells(f"{col_letter_s}4:{col_letter_e}4")
        c = ws.cell(row=4, column=col_start, value=label)
        c.font = F_KPI_LBL
        c.alignment = A_CENTER
        c.fill = FILL_PRIMARY_SOFT
        # 行5: 主值
        ws.merge_cells(f"{col_letter_s}5:{col_letter_e}5")
        c = ws.cell(row=5, column=col_start, value=val)
        c.font = F_KPI_VAL
        c.alignment = A_CENTER
        fill, font_color = _status_fill_font(status)
        c.fill = fill
        # 行6: 副值
        ws.merge_cells(f"{col_letter_s}6:{col_letter_e}6")
        c = ws.cell(row=6, column=col_start, value=sub)
        c.font = Font(name="Arial", size=10, bold=True, color=font_color)
        c.alignment = A_CENTER
        c.fill = fill
    ws.row_dimensions[4].height = 22
    ws.row_dimensions[5].height = 36
    ws.row_dimensions[6].height = 20

    # ── 各维度评估结果 (行8-14) ──
    ws.merge_cells("A8:H8")
    ws["A8"] = "各维度评估结果"
    ws["A8"].font = F_SECTION
    ws["A8"].alignment = A_LEFT
    ws.row_dimensions[8].height = 24

    headers = ["维度", "状态", "得分/通过率", "达标线", "说明"]
    for i, h in enumerate(headers):
        c = ws.cell(row=9, column=i+1, value=h)
        c.font = F_HEADER
        c.fill = FILL_HEADER
        c.alignment = A_CENTER
        c.border = BORDER_THIN
    ws.merge_cells("E9:H9")
    ws.row_dimensions[9].height = 28

    dim_rows = [
        ("质量指标（正确性）", "pass", "0.887 / 88.7%", "80%", "267/301 题正确"),
        ("质量总分", "pass", "0.872", "80%", "结果分 0.928；正确性 通过率 89%"),
        ("过程一致性", "warn", "0.810", "—", "3 题链条不一致；必达点 81%"),
        ("场景子步骤交付", "pass", "0.843", "80%", "必做步骤 28/30 通过；最弱步骤3 均分0.71"),
        ("运行稳定性", "warn", "—", "—", "结构：5题波动；性能：延迟通过率74%"),
    ]
    for r_idx, (dim, status, score, thr, desc) in enumerate(dim_rows, start=10):
        ws.cell(row=r_idx, column=1, value=dim).font = F_DATA_BOLD
        ws.cell(row=r_idx, column=1).alignment = A_LEFT
        ws.cell(row=r_idx, column=1).border = BORDER_THIN
        # 状态列着色
        c = ws.cell(row=r_idx, column=2, value=_status_label(status))
        fill, font_color = _status_fill_font(status)
        c.fill = fill
        c.font = Font(name="Arial", size=10, bold=True, color=font_color)
        c.alignment = A_CENTER
        c.border = BORDER_THIN
        for col_idx, val in enumerate([score, thr], start=3):
            c = ws.cell(row=r_idx, column=col_idx, value=val)
            c.font = F_DATA
            c.alignment = A_CENTER
            c.border = BORDER_THIN
        ws.merge_cells(start_row=r_idx, start_column=5, end_row=r_idx, end_column=8)
        c = ws.cell(row=r_idx, column=5, value=desc)
        c.font = F_DATA
        c.alignment = A_LEFT
        c.border = BORDER_THIN
        ws.row_dimensions[r_idx].height = 24

    # ── 核心发现 (行15-16) ──
    ws.merge_cells("A15:H15")
    ws["A15"] = "核心发现"
    ws["A15"].font = F_SECTION
    ws["A15"].alignment = A_LEFT
    ws.row_dimensions[15].height = 24

    ws.merge_cells("A16:H16")
    finding = ("正确性通过率88.7%（267/301），0分题目4题，低分题目（≤0.5）14题，"
               "接近及格题目（0.7-0.8）10题。过程一致性有3题链条不一致需关注，"
               "场景交付必达点28/30通过，运行稳定性延迟通过率74%偏高。")
    ws["A16"] = finding
    ws["A16"].font = F_DATA
    ws["A16"].alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
    ws.row_dimensions[16].height = 60

    # ── 报告配置项 (行18-19) ──
    ws.merge_cells("A18:H18")
    ws["A18"] = "报告配置项"
    ws["A18"].font = F_SECTION
    ws["A18"].alignment = A_LEFT
    ws.row_dimensions[18].height = 22

    config_lines = [
        f"任务ID: {TASK_META['task_id']}  |  Agent版本: {TASK_META['agent_version']}  |  评估模式: {TASK_META['evaluation_mode']}  |  推理轮次: {TASK_META['inference_rounds']}  |  评测模型: {TASK_META['eval_model']}  |  Temperature: {TASK_META['temperature']}",
        f"创建时间: {TASK_META['created_at']}  |  完成时间: {TASK_META['completed_at']}",
        f"维度开关: 质量✓ 过程一致性✓ 场景交付✓ 运行稳定性✓  |  质量指标: correctness, faithfulness, answer_relevancy  |  数据集: {TASK_META['dataset_name']}",
    ]
    for i, line in enumerate(config_lines):
        r = 19 + i
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=8)
        c = ws.cell(row=r, column=1, value=line)
        c.font = F_FOOTNOTE
        c.alignment = A_LEFT
        ws.row_dimensions[r].height = 18

    ws.sheet_view.showGridLines = False


def build_failed_questions_sheet(wb):
    """Sheet 2: 未通过题目汇总（以指标为粒度，含轮次和运行效能指标）。"""
    ws = wb.create_sheet("未通过题目汇总")
    _set_col_widths(ws, {"A": 6, "B": 10, "C": 8, "D": 14, "E": 8, "F": 14, "G": 12, "H": 90})

    headers = ["序号", "题目ID", "轮次", "指标", "得分", "推理时延(ms)", "Token消耗", "题目完整内容"]
    for i, h in enumerate(headers):
        c = ws.cell(row=1, column=i+1, value=h)
        c.font = F_HEADER
        c.fill = FILL_HEADER
        c.alignment = A_CENTER
        c.border = BORDER_THIN
    ws.row_dimensions[1].height = 28

    inference_rounds = TASK_META.get("inference_rounds", 1)

    row_num = 0
    for q in QUESTIONS:
        metric_scores = q.get("metric_scores") or {}
        metric_passed = q.get("metric_passed") or {}
        run_label = f"R{q.get('run_index', 0) + 1}" if inference_rounds > 1 else ""
        # 运行效能指标
        latency_val = metric_scores.get("runtime.inference_latency_ms")
        tokens_val = metric_scores.get("runtime.tokens_total")
        try:
            latency_str = f"{float(latency_val):.0f}" if latency_val is not None else "—"
        except (TypeError, ValueError):
            latency_str = "—"
        try:
            tokens_str = f"{float(tokens_val):.0f}" if tokens_val is not None else "—"
        except (TypeError, ValueError):
            tokens_str = "—"
        if not metric_scores:
            if not q["passed"]:
                row_num += 1
                r_idx = row_num + 1
                zebra = FILL_ZEBRA if r_idx % 2 == 0 else FILL_WHITE
                for col_idx, val in enumerate([row_num, f"Q{q['qid']}", run_label, "—", q["score"], latency_str, tokens_str, q["question"]], start=1):
                    c = ws.cell(row=r_idx, column=col_idx, value=val)
                    c.border = BORDER_THIN
                    if col_idx == 5:
                        s = q["score"]
                        if s < 0.6:
                            c.fill = FILL_RED
                            c.font = Font(name="Arial", size=11, bold=True, color=C_RED)
                        elif s < 0.8:
                            c.fill = FILL_AMBER
                            c.font = Font(name="Arial", size=11, bold=True, color=C_AMBER)
                        else:
                            c.fill = FILL_GREEN
                            c.font = Font(name="Arial", size=11, bold=True, color=C_GREEN)
                        c.alignment = A_CENTER
                    else:
                        c.fill = zebra
                        c.font = F_DATA
                        c.alignment = A_CENTER if col_idx <= 7 else A_LEFT_TOP
                ws.row_dimensions[r_idx].height = 80
            continue
        for mname, mscore in metric_scores.items():
            if metric_passed.get(mname, True):
                continue
            row_num += 1
            r_idx = row_num + 1
            zebra = FILL_ZEBRA if r_idx % 2 == 0 else FILL_WHITE
            display_name = METRIC_CN.get(mname, mname)
            for col_idx, val in enumerate([row_num, f"Q{q['qid']}", run_label, display_name, mscore, latency_str, tokens_str, q["question"]], start=1):
                c = ws.cell(row=r_idx, column=col_idx, value=val)
                c.border = BORDER_THIN
                if col_idx == 5:
                    if mscore < 0.6:
                        c.fill = FILL_RED
                        c.font = Font(name="Arial", size=11, bold=True, color=C_RED)
                    elif mscore < 0.8:
                        c.fill = FILL_AMBER
                        c.font = Font(name="Arial", size=11, bold=True, color=C_AMBER)
                    else:
                        c.fill = FILL_GREEN
                        c.font = Font(name="Arial", size=11, bold=True, color=C_GREEN)
                    c.alignment = A_CENTER
                else:
                    c.fill = zebra
                    c.font = F_DATA
                    c.alignment = A_CENTER if col_idx <= 7 else A_LEFT_TOP
            ws.row_dimensions[r_idx].height = 80

    ws.freeze_panes = "A2"
    ws.sheet_view.showGridLines = False


def build_question_details_sheet(wb):
    """Sheet 3: 题目详情（以指标为粒度，含轮次和运行效能指标）。"""
    ws = wb.create_sheet("题目详情")
    _set_col_widths(ws, {"A": 6, "B": 10, "C": 8, "D": 14, "E": 8, "F": 50, "G": 65, "H": 55, "I": 25})

    headers = ["序号", "题目ID", "轮次", "指标", "得分", "题目内容", "Agent回答", "标准答案", "评语"]
    for i, h in enumerate(headers):
        c = ws.cell(row=1, column=i+1, value=h)
        c.font = F_HEADER
        c.fill = FILL_HEADER
        c.alignment = A_CENTER
        c.border = BORDER_THIN
    ws.row_dimensions[1].height = 28

    inference_rounds = TASK_META.get("inference_rounds", 1)

    row_num = 0
    for q in QUESTIONS:
        metric_scores = q.get("metric_scores") or {}
        metric_reasons = q.get("metric_reasons") or {}
        run_label = f"R{q.get('run_index', 0) + 1}" if inference_rounds > 1 else ""
        if not metric_scores:
            row_num += 1
            r_idx = row_num + 1
            zebra = FILL_ZEBRA if r_idx % 2 == 0 else FILL_WHITE
            values = [row_num, f"Q{q['qid']}", run_label, "—", q["score"],
                      q["question"], q["agent_answer"], q["ground_truth"], q["eval_reason"]]
            for col_idx, val in enumerate(values, start=1):
                c = ws.cell(row=r_idx, column=col_idx, value=val)
                c.border = BORDER_THIN
                if col_idx == 5:
                    s = q["score"]
                    if s < 0.6:
                        c.fill = FILL_RED
                        c.font = Font(name="Arial", size=11, bold=True, color=C_RED)
                    elif s < 0.8:
                        c.fill = FILL_AMBER
                        c.font = Font(name="Arial", size=11, bold=True, color=C_AMBER)
                    else:
                        c.fill = FILL_GREEN
                        c.font = Font(name="Arial", size=11, bold=True, color=C_GREEN)
                    c.alignment = A_CENTER
                else:
                    c.fill = zebra
                    c.font = F_DATA
                    c.alignment = A_CENTER if col_idx <= 4 else A_LEFT_TOP
            ws.row_dimensions[r_idx].height = 120
            continue

        for mname, mscore in metric_scores.items():
            row_num += 1
            r_idx = row_num + 1
            zebra = FILL_ZEBRA if r_idx % 2 == 0 else FILL_WHITE
            reason = metric_reasons.get(mname) or q.get("eval_reason", "")
            values = [row_num, f"Q{q['qid']}", run_label, METRIC_CN.get(mname, mname), mscore,
                      q["question"], q["agent_answer"], q["ground_truth"], reason]
            for col_idx, val in enumerate(values, start=1):
                c = ws.cell(row=r_idx, column=col_idx, value=val)
                c.border = BORDER_THIN
                if col_idx == 5:
                    if mscore < 0.6:
                        c.fill = FILL_RED
                        c.font = Font(name="Arial", size=11, bold=True, color=C_RED)
                    elif mscore < 0.8:
                        c.fill = FILL_AMBER
                        c.font = Font(name="Arial", size=11, bold=True, color=C_AMBER)
                    else:
                        c.fill = FILL_GREEN
                        c.font = Font(name="Arial", size=11, bold=True, color=C_GREEN)
                    c.alignment = A_CENTER
                else:
                    c.fill = zebra
                    c.font = F_DATA
                    c.alignment = A_CENTER if col_idx <= 4 else A_LEFT_TOP
            ws.row_dimensions[r_idx].height = 120

    ws.freeze_panes = "A2"
    ws.sheet_view.showGridLines = False


def build_quality_sheet(wb):
    """Sheet: 质量指标 — 只展示维度结论。"""
    ws = wb.create_sheet("质量指标")
    _set_col_widths(ws, {"A": 22, "B": 60})

    # 标题
    ws.merge_cells("A1:B1")
    ws["A1"] = "质量指标维度结论"
    ws["A1"].font = F_SECTION
    ws["A1"].alignment = A_LEFT
    ws.row_dimensions[1].height = 26

    q = DIM_VERDICTS["quality"]
    rows = [
        ("状态", _status_label(q["status"])),
        ("质量总分", f"{QUALITY_SCORE['total_score']:.2f}"),
        ("结果分", f"{QUALITY_SCORE['result_score']:.2f}"),
        ("结论", q.get("headline", "—")),
    ]
    for i, (label, val) in enumerate(rows, start=2):
        zebra = FILL_ZEBRA if i % 2 == 0 else FILL_WHITE
        c = ws.cell(row=i, column=1, value=label)
        c.font = F_DATA_BOLD; c.alignment = A_LEFT; c.border = BORDER_THIN; c.fill = zebra
        c = ws.cell(row=i, column=2, value=val)
        c.font = F_DATA; c.alignment = A_LEFT_TOP; c.border = BORDER_THIN; c.fill = zebra
        if label == "状态":
            fill, font_color = _status_fill_font(q["status"])
            c.fill = fill
            c.font = Font(name="Arial", size=10, bold=True, color=font_color)
        ws.row_dimensions[i].height = 22

    ws.sheet_view.showGridLines = False


def build_process_consistency_sheet(wb):
    """Sheet: 过程一致性 — 只展示维度结论。"""
    ws = wb.create_sheet("过程一致性")
    _set_col_widths(ws, {"A": 22, "B": 60})

    # 标题
    ws.merge_cells("A1:B1")
    ws["A1"] = "过程一致性维度结论"
    ws["A1"].font = F_SECTION
    ws["A1"].alignment = A_LEFT
    ws.row_dimensions[1].height = 26

    pc = DIM_VERDICTS["process_consistency"]
    m = pc["metrics"]
    rows = [
        ("状态", _status_label(pc["status"])),
        ("过程分", f"{pc['score']:.2f}"),
        ("链条通过率", f"{m.get('chain_pass_rate')}%"),
        ("绿/黄/红", f"{m.get('green')}/{m.get('yellow')}/{m.get('red')}"),
        ("结论", pc.get("headline", "—")),
    ]
    for i, (label, val) in enumerate(rows, start=2):
        zebra = FILL_ZEBRA if i % 2 == 0 else FILL_WHITE
        c = ws.cell(row=i, column=1, value=label)
        c.font = F_DATA_BOLD; c.alignment = A_LEFT; c.border = BORDER_THIN; c.fill = zebra
        c = ws.cell(row=i, column=2, value=val)
        c.font = F_DATA; c.alignment = A_LEFT_TOP; c.border = BORDER_THIN; c.fill = zebra
        if label == "状态":
            fill, font_color = _status_fill_font(pc["status"])
            c.fill = fill
            c.font = Font(name="Arial", size=10, bold=True, color=font_color)
        ws.row_dimensions[i].height = 22

    ws.sheet_view.showGridLines = False


def build_scenario_steps_sheet(wb):
    """Sheet: 场景子步骤交付 — 只展示维度结论。"""
    ws = wb.create_sheet("场景子步骤交付")
    _set_col_widths(ws, {"A": 22, "B": 60})

    sd = DIM_VERDICTS["scenario_delivery"]
    m = sd["metrics"]

    # 标题
    ws.merge_cells("A1:B1")
    ws["A1"] = "场景子步骤交付维度结论"
    ws["A1"].font = F_SECTION
    ws["A1"].alignment = A_LEFT
    ws.row_dimensions[1].height = 26

    rows = [
        ("状态", _status_label(sd["status"])),
        ("场景综合分", f"{sd['score']:.2f}"),
        ("必做步骤通过率", f"{m.get('required_pass_rate')}% ({m.get('required_passed')}/{m.get('required_total')})"),
    ]
    if m.get("volatile_question_count"):
        rows.append(("波动题数", str(m["volatile_question_count"])))
    rows.append(("结论", sd.get("headline", "—")))

    for i, (label, val) in enumerate(rows, start=2):
        zebra = FILL_ZEBRA if i % 2 == 0 else FILL_WHITE
        c = ws.cell(row=i, column=1, value=label)
        c.font = F_DATA_BOLD; c.alignment = A_LEFT; c.border = BORDER_THIN; c.fill = zebra
        c = ws.cell(row=i, column=2, value=val)
        c.font = F_DATA; c.alignment = A_LEFT_TOP; c.border = BORDER_THIN; c.fill = zebra
        if label == "状态":
            fill, font_color = _status_fill_font(sd["status"])
            c.fill = fill
            c.font = Font(name="Arial", size=10, bold=True, color=font_color)
        ws.row_dimensions[i].height = 22

    ws.sheet_view.showGridLines = False


def build_stability_sheet(wb):
    """Sheet: 运行稳定性 — 只展示维度结论。"""
    ws = wb.create_sheet("运行稳定性")
    _set_col_widths(ws, {"A": 22, "B": 60})

    rs = DIM_VERDICTS["runtime_stability"]

    # 标题
    ws.merge_cells("A1:B1")
    ws["A1"] = "运行稳定性维度结论"
    ws["A1"].font = F_SECTION
    ws["A1"].alignment = A_LEFT
    ws.row_dimensions[1].height = 26

    # 使用 performance 子维度的状态作为主状态（与 web 端一致）
    perf = rs["performance"]
    perf_status = perf.get("status", "na")
    struct = rs.get("structural") or {}
    qs_sub = rs.get("quality_stability") or {}
    perf_metrics = perf.get("metrics") or {}

    rows = [
        ("状态", _status_label(perf_status)),
    ]
    if struct.get("status") not in ("na", "disabled"):
        rows.append(("结构稳定性", _status_label(struct.get("status", "na"))))
    if perf.get("status") not in ("na", "disabled"):
        if perf_metrics.get("latency_pass_rate") is not None:
            rows.append(("延迟通过率", f"{perf_metrics['latency_pass_rate']}%"))
        if perf_metrics.get("tokens_pass_rate") is not None:
            rows.append(("Token通过率", f"{perf_metrics['tokens_pass_rate']}%"))
    if qs_sub.get("status") not in ("na", "disabled"):
        qsm = qs_sub.get("metrics") or {}
        if qsm.get("pass_rate") is not None:
            rows.append(("多轮正确率", f"{qsm['pass_rate']}%"))
    rows.append(("结论", rs.get("headline", "—")))

    for i, (label, val) in enumerate(rows, start=2):
        zebra = FILL_ZEBRA if i % 2 == 0 else FILL_WHITE
        c = ws.cell(row=i, column=1, value=label)
        c.font = F_DATA_BOLD; c.alignment = A_LEFT; c.border = BORDER_THIN; c.fill = zebra
        c = ws.cell(row=i, column=2, value=val)
        c.font = F_DATA; c.alignment = A_LEFT_TOP; c.border = BORDER_THIN; c.fill = zebra
        if label == "状态":
            fill, font_color = _status_fill_font(perf_status)
            c.fill = fill
            c.font = Font(name="Arial", size=10, bold=True, color=font_color)
        ws.row_dimensions[i].height = 22

    ws.sheet_view.showGridLines = False


def build_findings_sheet(wb):
    """Sheet 8: 关键发现与建议。"""
    ws = wb.create_sheet("关键发现与建议")
    _set_col_widths(ws, {"A": 6, "B": 28, "C": 18, "D": 60})

    ws.merge_cells("A1:D1")
    ws["A1"] = "关键发现与改进建议"
    ws["A1"].font = F_TITLE
    ws["A1"].alignment = A_LEFT
    ws.row_dimensions[1].height = 32

    # 关键发现
    ws.merge_cells("A3:D3")
    ws["A3"] = "关键发现"
    ws["A3"].font = F_SECTION
    ws["A3"].alignment = A_LEFT
    ws.row_dimensions[3].height = 24

    findings = [
        ("0分题目4题", "Agent回答为空或完全错误，涉及履约调拨、优先级计算等场景，脚本调用或推理逻辑存在缺陷。"),
        ("低分题目（≤0.5分）14题", "这些题目需要深入排查Agent的推理链路和脚本调用问题。"),
        ("正确性通过率88.7%", "267/301题通过，已达标。"),
        ("接近及格题目（0.7-0.8分）10题", "通过针对性修复可较快达到通过线，是短期优化重点。"),
        ("质量总分0.872", "结果分0.928；过程一致性 0.810（3题链条不一致）；场景交付 0.843；稳定性延迟偏高。"),
    ]
    for i, (b, d) in enumerate(findings, start=4):
        c = ws.cell(row=i, column=1, value=i-3)
        c.font = F_DATA_BOLD; c.alignment = A_CENTER; c.border = BORDER_THIN
        c.fill = FILL_PRIMARY_SOFT
        ws.merge_cells(start_row=i, start_column=2, end_row=i, end_column=3)
        c = ws.cell(row=i, column=2, value=b)
        c.font = F_DATA_BOLD; c.alignment = A_LEFT; c.border = BORDER_THIN; c.fill = FILL_PRIMARY_SOFT
        ws.cell(row=i, column=3).border = BORDER_THIN; ws.cell(row=i, column=3).fill = FILL_PRIMARY_SOFT
        c = ws.cell(row=i, column=4, value=d)
        c.font = F_DATA; c.alignment = A_LEFT_TOP; c.border = BORDER_THIN
        ws.row_dimensions[i].height = 40

    # 改进建议
    ws.merge_cells(f"A{len(findings)+6}:D{len(findings)+6}")
    ws.cell(row=len(findings)+6, column=1, value="改进建议").font = F_SECTION
    ws.cell(row=len(findings)+6, column=1).alignment = A_LEFT
    ws.row_dimensions[len(findings)+6].height = 24

    suggestions = [
        ("修复0分题目", "4道0分题目需排查脚本调用失败和推理逻辑错误。"),
        ("提升接近及格题目", "10道0.7-0.8分题目通过细节优化即可通过，投入产出比最高。"),
        ("深入分析低分根因", "14道低分题目需逐题分析Agent回答与标准答案的差异。"),
        ("关注过程一致性波动", "3题存在多轮链条不一致，需排查推理路径稳定性。"),
        ("优化推理延迟", "延迟通过率74%低于80%目标，需排查慢请求和重试逻辑。"),
    ]
    for i, (b, d) in enumerate(suggestions, start=len(findings)+7):
        c = ws.cell(row=i, column=1, value=i-len(findings)-6)
        c.font = F_DATA_BOLD; c.alignment = A_CENTER; c.border = BORDER_THIN
        c.fill = FILL_PRIMARY_SOFT
        ws.merge_cells(start_row=i, start_column=2, end_row=i, end_column=3)
        c = ws.cell(row=i, column=2, value=b)
        c.font = F_DATA_BOLD; c.alignment = A_LEFT; c.border = BORDER_THIN; c.fill = FILL_PRIMARY_SOFT
        ws.cell(row=i, column=3).border = BORDER_THIN; ws.cell(row=i, column=3).fill = FILL_PRIMARY_SOFT
        c = ws.cell(row=i, column=4, value=d)
        c.font = F_DATA; c.alignment = A_LEFT_TOP; c.border = BORDER_THIN
        ws.row_dimensions[i].height = 40

    # 脚注
    foot_row = len(findings) + len(suggestions) + 9
    ws.merge_cells(start_row=foot_row, start_column=1, end_row=foot_row, end_column=4)
    c = ws.cell(row=foot_row, column=1,
               value=f"报告生成时间: {TASK_META['eval_time']}  |  数据来源: Onto-Eval 本体评测平台  |  任务ID: {TASK_META['task_id']}")
    c.font = F_FOOTNOTE; c.alignment = A_LEFT
    ws.row_dimensions[foot_row].height = 18

    ws.sheet_view.showGridLines = False


def main():
    wb = Workbook()
    # 删除默认 sheet
    wb.remove(wb.active)

    build_overview_sheet(wb)
    build_failed_questions_sheet(wb)
    build_question_details_sheet(wb)
    build_quality_sheet(wb)
    build_process_consistency_sheet(wb)
    build_scenario_steps_sheet(wb)
    build_stability_sheet(wb)
    build_findings_sheet(wb)

    out_path = os.path.join(os.path.dirname(__file__), "demo_eval_report_v2.xlsx")
    wb.save(out_path)
    print(f"Demo report generated: {out_path}")
    print(f"Sheets: {wb.sheetnames}")


if __name__ == "__main__":
    main()
