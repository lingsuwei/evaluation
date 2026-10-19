"""评测模式（在线 / 离线）与能力边界。"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

from evaluators.runtime_metrics import is_runtime_metric

EVALUATION_MODE_ONLINE = "online"
EVALUATION_MODE_OFFLINE = "offline"

ONLINE_CAPABILITIES = ["quality", "runtime", "trace", "scenario", "checkpoint"]
OFFLINE_CAPABILITIES = ["quality"]


def get_evaluation_mode(task: Any) -> str:
    """返回 online | offline；优先 evaluation_mode 列，否则由 agent_id 推断。"""
    mode = (getattr(task, "evaluation_mode", None) or "").strip().lower()
    if mode in (EVALUATION_MODE_ONLINE, EVALUATION_MODE_OFFLINE):
        return mode
    return EVALUATION_MODE_ONLINE if getattr(task, "agent_id", None) else EVALUATION_MODE_OFFLINE


def is_online_task(task: Any) -> bool:
    return get_evaluation_mode(task) == EVALUATION_MODE_ONLINE


def is_offline_task(task: Any) -> bool:
    return not is_online_task(task)


def task_has_process_eval_runs(task: Any) -> bool:
    """离线任务是否已写入 evaluation_mode='offline' 的 AgentRun（即已跑过 langfuse 过程回放）。"""
    if not is_offline_task(task):
        return False
    return bool(getattr(task, "process_eval_completed", False))


def get_task_capabilities(task: Any) -> List[str]:
    base = list(ONLINE_CAPABILITIES if is_online_task(task) else OFFLINE_CAPABILITIES)
    if is_offline_task(task):
        has_process_eval = task_has_process_eval_runs(task)
        has_langfuse_source = bool(getattr(task, "langfuse_source", None))
        if has_process_eval or has_langfuse_source:
            for extra in ("scenario", "checkpoint"):
                if extra not in base:
                    base.append(extra)
    # 维度开关关闭时，从 capabilities 剔除对应能力（前端据此隐藏区域/Tab）
    # None 视为未设置（默认启用），仅显式 False 才剔除
    # 运行效能（runtime）独立于稳定性维度，不受 enable_stability 控制
    if getattr(task, "auto_scenario_score", True) is False:
        base = [c for c in base if c != "scenario"]
    if getattr(task, "enable_process_consistency", True) is False:
        base = [c for c in base if c != "checkpoint"]
    return base


def get_task_dimension_flags(task: Any) -> Dict[str, bool]:
    """返回任务创建时用户勾选的评估维度开关快照(未经模式/数据源过滤)。

    与 get_task_capabilities 的区别:capabilities 是经过在线/离线模式、
    langfuse 回放状态等多重过滤后的"实际可评维度";dimension_flags 仅反映
    用户配置意图,用于评估历史记录"当时勾选了哪些维度"。
    quality 维度无开关,恒为 True;None 视为未设置(默认启用),与 capabilities
    的过滤语义对齐——仅显式 False 才视为关闭。
    """
    return {
        "quality": True,
        "scenario": getattr(task, "auto_scenario_score", True) is not False,
        "process_consistency": getattr(task, "enable_process_consistency", True) is not False,
        "stability": getattr(task, "enable_stability", True) is not False,
    }


def filter_quality_metrics(
    metrics: Optional[Sequence[str]],
    *,
    allowed: Iterable[str],
) -> List[str]:
    """仅保留配置内、非运行时的质量指标。"""
    source = list(metrics or [])
    return [
        m.strip()
        for m in source
        if m and m.strip() in allowed and not is_runtime_metric(m.strip())
    ]


def resolve_offline_metrics_to_run(
    selected_metrics: Optional[Sequence[str]],
    enabled_metrics: Sequence[str],
    metrics_config: dict,
) -> List[str]:
    if selected_metrics:
        base = [m.strip() for m in selected_metrics if m and m.strip()]
    else:
        base = [m.strip() for m in enabled_metrics if m and m.strip()]
    return [m for m in base if m in metrics_config and not is_runtime_metric(m)]


def validate_offline_evaluate_request(
    *,
    selected_metrics: Optional[List[str]],
    metrics_config: dict,
    enabled_metrics: Sequence[str],
    inference_rounds: int,
) -> tuple[List[str], int]:
    """离线创建任务：剔除 runtime 指标，固定 inference_rounds=1。"""
    metrics = resolve_offline_metrics_to_run(
        selected_metrics, enabled_metrics, metrics_config
    )
    if selected_metrics:
        rejected = [
            m.strip()
            for m in selected_metrics
            if m and is_runtime_metric(m.strip())
        ]
        if rejected:
            raise ValueError(
                f"离线评估不支持运行效能指标：{', '.join(rejected)}"
            )
    return metrics, 1
