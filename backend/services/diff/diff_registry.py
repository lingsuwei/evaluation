"""Diff Calculator 注册表（P3 §10.4.1）。"""
from __future__ import annotations

from typing import Dict, Iterable, List, Type

from services.diff.base import DiffCalculator
from services.diff.checkpoint_diff_calculator import CheckpointDiffCalculator
from services.diff.cost_diff_calculator import CostDiffCalculator
from services.diff.diff_types import P3_DEFAULT_DIFF_TYPES, VERSION_EXPERIMENT_DIFF_TYPES, WORKFLOW_EXPERIMENT_DIFF_TYPES
from services.diff.event_agg_diff_calculator import EventAggDiffCalculator
from services.diff.metric_diff_calculator import MetricDiffCalculator
from services.diff.outcome_diff_calculator import OutcomeDiffCalculator
from services.diff.role_roster_diff_calculator import RoleRosterDiffCalculator
from services.diff.semantic_diff_calculator import SemanticDiffCalculator
from services.diff.structural_diff_calculator import StructuralDiffCalculator
from services.diff.workflow_lift_diff_calculator import WorkflowLiftDiffCalculator

DIFF_CALCULATORS: Dict[str, Type[DiffCalculator]] = {
    OutcomeDiffCalculator.diff_type: OutcomeDiffCalculator,
    CostDiffCalculator.diff_type: CostDiffCalculator,
    CheckpointDiffCalculator.diff_type: CheckpointDiffCalculator,
    EventAggDiffCalculator.diff_type: EventAggDiffCalculator,
    MetricDiffCalculator.diff_type: MetricDiffCalculator,
    SemanticDiffCalculator.diff_type: SemanticDiffCalculator,
    StructuralDiffCalculator.diff_type: StructuralDiffCalculator,
    RoleRosterDiffCalculator.diff_type: RoleRosterDiffCalculator,
    WorkflowLiftDiffCalculator.diff_type: WorkflowLiftDiffCalculator,
}


def resolve_diff_types(requested: Iterable[str] | None) -> List[str]:
    if not requested:
        return list(P3_DEFAULT_DIFF_TYPES)
    unknown = [t for t in requested if t not in DIFF_CALCULATORS]
    if unknown:
        raise ValueError(f"unknown diff_type: {', '.join(unknown)}")
    return list(requested)


def resolve_experiment_diff_types(requested: Iterable[str] | None) -> List[str]:
    """版本实验默认附带 semantic_diff + structural_diff。"""
    if not requested:
        return list(VERSION_EXPERIMENT_DIFF_TYPES)
    return resolve_diff_types(requested)


def resolve_workflow_diff_types(requested: Iterable[str] | None) -> List[str]:
    """工作流实验默认附带 role_roster_diff（M-W3）。"""
    if not requested:
        return list(WORKFLOW_EXPERIMENT_DIFF_TYPES)
    return resolve_diff_types(requested)


def get_calculator(diff_type: str) -> DiffCalculator:
    cls = DIFF_CALCULATORS.get(diff_type)
    if not cls:
        raise ValueError(f"unknown diff_type: {diff_type}")
    return cls()
