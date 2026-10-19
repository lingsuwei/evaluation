"""
按"时间窗 + 业务 trace 过滤"在 Langfuse 项目里定位业务 agent 原始 trace。

本模块从 ``trace_sync_worker._find_trace_by_time_window`` 抽出，供以下两处复用：
    - ``trace_sync_worker``：trace_id 直接查询失败时，按 run.started_at 时间窗
      重新列 traces，选时间最接近的一条作为该 run 的真实业务 trace。
    - ``langfuse_answer_source.fetch_langfuse_answer_map``：当
      ``tags=task:xxx`` 路径拿不到任何业务 trace（onto-agent 端没给
      业务 trace 打 task tag）时，按 task 下各 run 的 started_at 时间窗
      拉业务 trace，让离线评估预览能拿到业务 agent 的真实 output。

业务 trace 过滤规则集中放在 ``services.langfuse_trace_filter``，与前端
``LangfuseTraceExplore.isEvalReportedTrace`` 保持一致：name 以
"onto-eval/" 开头 或 tags 含 "onto-eval" 的 trace 视为 onto-eval 自
己的评估包装 trace，必须剔除。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from logger import log
from services.langfuse_client import LangfuseClient
from services.langfuse_trace_filter import filter_out_eval_reported_traces

# 列表 traces 的分页尺寸；时间窗 fallback 不需要拉太多页。
_FALLBACK_PAGE_LIMIT = 50
_FALLBACK_MAX_PAGES = 3
# 时间窗前后延伸的宽度（秒）。
# 从 600s 缩小到 180s：10 轮推理间隔通常 <3min，±180s 足以覆盖 SDK 上报漂移，
# 同时大幅减少多 run 窗口重叠导致的误匹配。
_TIME_WINDOW_PADDING_SECONDS = 180


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def find_trace_by_time_window(
    client: LangfuseClient,
    anchor_ts: Any,
    project_label: str = "",
    *,
    timeout: Optional[float] = None,
    session_id: Optional[str] = None,
    claimed_trace_ids: Optional[Set[str]] = None,
    end_ts: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """在 Langfuse 项目中按时间窗定位一条最接近的业务 trace。

    Args:
        client: LangfuseClient 实例（已绑定项目）。
        anchor_ts: 锚点时间（agent_runs.started_at）。
        project_label: 仅用于日志上下文。
        timeout: 请求超时秒数（None 用默认值）。
        session_id: 可选的 Langfuse session_id 过滤，缩小候选集。
        claimed_trace_ids: 已被同批其他 run 占用的 trace_id 集合，
            命中的 trace 不会被重复分配给当前 run。
        end_ts: 可选的推理结束时间（agent_runs.ended_at）。
            提供时窗口变为 [anchor_ts - padding, end_ts + padding]，
            覆盖整个推理过程，适配长推理（30min+）场景。

    Returns:
        最接近 anchor 的那条 trace dict（已过滤评估包装 trace + 已排除被占用 trace），
        没有命中时返回 None。
    """
    anchor = _parse_ts(anchor_ts)
    if anchor is None:
        log(
            "WARN",
            "TRACE_RESOLVER",
            "anchor_ts 解析失败，跳过时间窗 fallback",
            project=project_label,
        )
        return None

    # 自适应窗口：长推理时覆盖 [started_at, ended_at] 全程
    end_dt = _parse_ts(end_ts) if end_ts else None
    from_ts = datetime.fromtimestamp(
        anchor.timestamp() - _TIME_WINDOW_PADDING_SECONDS, tz=timezone.utc
    ).isoformat()
    if end_dt is not None:
        to_ts = datetime.fromtimestamp(
            end_dt.timestamp() + _TIME_WINDOW_PADDING_SECONDS, tz=timezone.utc
        ).isoformat()
    else:
        to_ts = datetime.fromtimestamp(
            anchor.timestamp() + _TIME_WINDOW_PADDING_SECONDS, tz=timezone.utc
        ).isoformat()

    best: Optional[Dict[str, Any]] = None
    best_diff: Optional[float] = None
    try:
        for page in range(1, _FALLBACK_MAX_PAGES + 1):
            data = await client.list_traces(
                limit=_FALLBACK_PAGE_LIMIT,
                page=page,
                from_timestamp=from_ts,
                to_timestamp=to_ts,
                session_id=session_id,
                timeout=timeout,
            )
            batch = data.get("data", []) or []
            # 剔除评估包装 trace：它的 output 字段是 None，绝对不能作为业务
            # trace 同步进 runtime facts。
            batch = filter_out_eval_reported_traces(batch)
            for t in batch:
                tid = (t.get("id") or "").strip()
                # 跳过已被同批其他 run 占用的 trace
                if claimed_trace_ids and tid and tid in claimed_trace_ids:
                    continue
                ts = t.get("timestamp") or t.get("createdAt")
                t_dt = _parse_ts(ts)
                if t_dt is None:
                    continue
                diff = abs((t_dt - anchor).total_seconds())
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best = t
            meta = data.get("meta", {}) or {}
            total_pages = meta.get("totalPages", 0)
            if total_pages and page >= total_pages:
                break
    except Exception as e:
        log("WARN", "TRACE_RESOLVER", "list_traces 失败", error=str(e))
        return None

    if best is not None:
        log(
            "INFO",
            "TRACE_RESOLVER",
            "时间窗 fallback 命中",
            project=project_label,
            trace_id=best.get("id"),
            diff_seconds=best_diff,
            session_id=session_id or "(none)",
        )
    return best


async def find_traces_by_run_windows(
    client: LangfuseClient,
    runs: Sequence[Dict[str, Any]],
    project_label: str = "",
    *,
    timeout: Optional[float] = None,
) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """按一组 run 的 started_at 时间窗分别定位业务 trace。

    Args:
        client: LangfuseClient 实例。
        runs: 每个元素需要至少含 ``id``、``task_id``、``question_id``、
            ``started_at`` 字段。
        project_label: 仅用于日志上下文。

    Returns:
        列表，每项是 ``(run, trace)``，顺序与 runs 中 run 的时间升序一致；
        某个 run 时间窗内没命中业务 trace 时不入列。
    """
    results: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    claimed: Set[str] = set()
    for run in runs:
        anchor = run.get("started_at")
        if not anchor:
            continue
        trace = await find_trace_by_time_window(
            client=client,
            anchor_ts=anchor,
            project_label=project_label,
            timeout=timeout,
            session_id=run.get("session_id"),
            claimed_trace_ids=claimed,
            end_ts=run.get("ended_at"),
        )
        if trace is None:
            continue
        tid = (trace.get("id") or "").strip()
        if tid:
            claimed.add(tid)
        results.append((run, trace))
    return results
