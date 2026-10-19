"""Diff 类型常量（P3 §10.3 / §10.8）。"""
from __future__ import annotations

OUTCOME_DIFF = "outcome_diff"
COST_DIFF = "cost_diff"
CHECKPOINT_DIFF = "checkpoint_diff"
EVENT_AGG_DIFF = "event_agg_diff"
SEMANTIC_DIFF = "semantic_diff"
STRUCTURAL_DIFF = "structural_diff"
METRIC_DIFF = "metric_diff"  # 兼容别名

# M-W3: 工作流对比专用 diff 类型
ROLE_ROSTER_DIFF = "role_roster_diff"  # 角色花名册对比（成员增减 / 角色变更）
WORKFLOW_LIFT_DIFF = "workflow_lift_diff"  # 工作流 vs 单 Agent 提升量

P3_DEFAULT_DIFF_TYPES = (
    OUTCOME_DIFF,
    COST_DIFF,
    CHECKPOINT_DIFF,
    EVENT_AGG_DIFF,
)

# 版本实验默认：P3 四类 + semantic + structural（P5-E+）
VERSION_EXPERIMENT_DIFF_TYPES = P3_DEFAULT_DIFF_TYPES + (
    SEMANTIC_DIFF,
    STRUCTURAL_DIFF,
)

# M-W3: 工作流版本对比默认附带 role_roster_diff（成员结构变化是工作流版本演进的核心关注点）
WORKFLOW_EXPERIMENT_DIFF_TYPES = VERSION_EXPERIMENT_DIFF_TYPES + (
    ROLE_ROSTER_DIFF,
)

SCHEMA_VERSION = "1.0"
