"""
Trace Sync Worker（P1.5）：异步拉取 Langfuse trace → Runtime Fact Builder。

分析报表应读 agent_runs 等物化表，而非在请求路径扫 Langfuse Public API。

查找策略（三级递进）：
  Level 1: get_trace(trace_id) 直查（最快路径）
  Level 2: list_traces(session_id=...) + metadata.run_id 精确匹配
  Level 3: find_trace_by_time_window 时间窗 fallback（兜底，带 session_id 过滤）
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    LANGFUSE_ENABLED,
    TRACE_SYNC_INCOMPLETE_LOOKBACK_HOURS,
    TRACE_SYNC_MAX_ATTEMPTS,
    TRACE_SYNC_MAX_RERUN,
    TRACE_SYNC_MAX_WAIT_SEC,
    TRACE_SYNC_MIN_INGESTION_DELAY_SEC,
    TRACE_SYNC_STABILITY_WINDOW_SEC,
    TRACE_SYNC_SWEEP_BATCH_SIZE,
    TRACE_SYNC_SWEEP_INTERVAL_SEC,
)
from database import AgentRun, TraceSyncJob, async_session
from logger import log
from services.agent_run_service import get_agent_run
from services.langfuse_client import LANGFUSE_WORKER_TIMEOUT, get_langfuse_client
from services.langfuse_project_registry import ProjectNotFoundError
from services.langfuse_trace_filter import filter_out_eval_reported_traces
from services.langfuse_trace_resolver import find_trace_by_time_window
from services.runtime_fact_builder import build_facts_for_trace, count_observations
from services.trace_sync_failure_category import classify_failure
from services.trace_sync_observability import get_trace_sync_queue_stats

import asyncio

# trace sync 并发控制
_TRACE_SYNC_SEMAPHORE = asyncio.Semaphore(5)

# synced_incomplete 补扫限频时间戳
_last_sweep_at: float = 0.0


async def _fetch_pending_jobs(
    session: AsyncSession,
    limit: int,
) -> List[TraceSyncJob]:
    result = await session.execute(
        select(TraceSyncJob)
        .where(TraceSyncJob.status == "pending")
        .order_by(TraceSyncJob.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


# ── Observation 稳定性检查 ──
# onto-agent 推理结束后异步向 Langfuse 上报 observations，可能持续数分钟。
# 若 sync worker 在上传过程中拉取 trace，只会拿到部分 observations。
# 通过连续拉取对比 obs count，确认上传完成后再处理。

_STABILITY_PREFIX = "STABILITY_CHECK:"


def _parse_stability_state(last_error: Optional[str]) -> Optional[Dict[str, Any]]:
    """从 job.last_error 解析稳定性检查状态 JSON。

    格式：``STABILITY_CHECK: {"prev_obs_count": N, "first_check_at": "ISO", "last_change_at": "ISO"}``
    解析失败或前缀不匹配时返回 None（当作首次检查）。
    """
    if not last_error:
        return None
    text = last_error.strip()
    if not text.startswith(_STABILITY_PREFIX):
        return None
    try:
        payload = json.loads(text[len(_STABILITY_PREFIX):].strip())
        return payload if isinstance(payload, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _check_observation_stability(
    job: TraceSyncJob,
    trace: Dict[str, Any],
    now: datetime,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """检查 trace 的 observation 数量是否已稳定（onto-agent 上传完成）。

    返回 ``(should_defer, reason, metadata_to_store)``：
    - should_defer=True 时，metadata 非空，需写入 ``job.last_error`` 并延迟重试。
    - should_defer=False 时，metadata=None，可继续处理 trace。

    算法：
      首次拉取：总是 defer，记录 prev_obs_count / first_check_at / last_change_at。
      后续拉取：
        - obs_count 增长 → 仍在上传，更新 prev_count 和 last_change_at，defer。
        - obs_count 不变 且距上次变化 ≥ STABILITY_WINDOW_SEC → 稳定，处理。
        - obs_count 不变 且距上次变化 < STABILITY_WINDOW_SEC → 可能还在上传，defer。
        - 距首次检查 ≥ MAX_WAIT_SEC → 超时强制处理。
    """
    current_count = count_observations(trace)
    prev_state = _parse_stability_state(job.last_error)

    if prev_state is None:
        # 首次检查：总是 defer 以启用稳定性验证
        metadata: Dict[str, Any] = {
            "prev_obs_count": current_count,
            "first_check_at": now.isoformat(),
            "last_change_at": now.isoformat(),
        }
        return True, f"首次拉取 obs_count={current_count}，defer 等待稳定性验证", metadata

    prev_count = prev_state.get("prev_obs_count", 0)
    first_check_str = prev_state.get("first_check_at") or now.isoformat()
    last_change_str = prev_state.get("last_change_at") or now.isoformat()

    try:
        first_check_at = datetime.fromisoformat(first_check_str)
    except (ValueError, TypeError):
        first_check_at = now
    try:
        last_change_at = datetime.fromisoformat(last_change_str)
    except (ValueError, TypeError):
        last_change_at = now

    wait_elapsed = (now - first_check_at).total_seconds()
    stable_elapsed = (now - last_change_at).total_seconds()

    # 超时强制处理
    if wait_elapsed >= TRACE_SYNC_MAX_WAIT_SEC:
        return (
            False,
            f"达到最大等待 {wait_elapsed:.0f}s >= {TRACE_SYNC_MAX_WAIT_SEC}s，"
            f"强制处理 obs_count={current_count}",
            None,
        )

    # obs count 仍在增长
    if current_count > prev_count:
        metadata = {
            "prev_obs_count": current_count,
            "first_check_at": first_check_str,
            "last_change_at": now.isoformat(),
        }
        return (
            True,
            f"obs_count 增长 {prev_count} → {current_count}，仍在上传，defer",
            metadata,
        )

    # obs count 不变，检查稳定性窗口
    if stable_elapsed >= TRACE_SYNC_STABILITY_WINDOW_SEC:
        return (
            False,
            f"obs_count={current_count} 已稳定 {stable_elapsed:.0f}s，处理",
            None,
        )

    # obs count 不变但稳定性窗口未满
    metadata = {
        "prev_obs_count": current_count,
        "first_check_at": first_check_str,
        "last_change_at": last_change_str,
    }
    return (
        True,
        f"obs_count={current_count} 未变但稳定时间 {stable_elapsed:.0f}s "
        f"< {TRACE_SYNC_STABILITY_WINDOW_SEC}s，defer",
        metadata,
    )


async def _find_trace_by_session(
    client,
    run: AgentRun,
    *,
    timeout: Optional[float] = None,
    claimed_trace_ids: Optional[Set[str]] = None,
    claimed_lock: Optional[asyncio.Lock] = None,
) -> Optional[Dict[str, Any]]:
    """Level 2: 按 onto_agent_session_id 查 Langfuse traces，再按 run_id 精确匹配。

    比 Level 3 时间窗 fallback 精确得多：
    - session_id 把候选集从"全项目"缩小到"同一 session"（通常 = 同一题）
    - run_id 在 metadata 中精确匹配到具体哪一轮

    并发安全：claimed_trace_ids + claimed_lock 保证同批 drain 中不会
    多个 job 选中同一条 trace。时间戳近似时跳过 run_id 不匹配的 trace。
    """
    session_id = (getattr(run, "onto_agent_session_id", None) or "").strip()
    if not session_id:
        return None

    try:
        data = await client.list_traces(
            session_id=session_id,
            limit=50,
            timeout=timeout,
        )
    except Exception as e:
        log(
            "WARN",
            "TRACE_SYNC",
            "Level 2 session_id 查询失败",
            session_id=session_id,
            error=str(e)[:300],
        )
        return None

    batch = data.get("data", []) or []
    batch = filter_out_eval_reported_traces(batch)
    if not batch:
        return None

    onto_run_id = (getattr(run, "onto_agent_run_id", None) or "").strip()

    def _select(batch_traces: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """从候选 batch 中选择最佳 trace（纯 CPU，无 I/O，可在锁内调用）。"""
        # 优先按 metadata.run_id 精确匹配
        if onto_run_id:
            for t in batch_traces:
                meta = t.get("metadata") or {}
                if str(meta.get("run_id") or "").strip() == onto_run_id:
                    return t

        # run_id 未匹配 → 按时间戳近似，但跳过 run_id 明确不匹配的 trace
        from services.langfuse_trace_resolver import _parse_ts

        anchor = _parse_ts(run.started_at)
        end = _parse_ts(getattr(run, "ended_at", None)) if getattr(run, "ended_at", None) else None
        best = None
        best_diff = None
        for t in batch_traces:
            meta = t.get("metadata") or {}
            t_run_id = str(meta.get("run_id") or "").strip()
            # 跳过 run_id 明确属于其他 run 的 trace（防止跨 run 误匹配）
            if t_run_id and onto_run_id and t_run_id != onto_run_id:
                continue

            ts = t.get("timestamp") or t.get("createdAt")
            if not ts:
                continue
            t_dt = _parse_ts(ts)
            if t_dt is None:
                continue
            try:
                if anchor and end and anchor <= t_dt <= end:
                    diff = 0
                elif anchor:
                    diff = abs((t_dt - anchor).total_seconds())
                else:
                    diff = 0
            except Exception:
                continue
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best = t

        if best:
            log(
                "INFO",
                "TRACE_SYNC",
                "Level 2 session_id 命中（run_id 未匹配，用时间戳近似）",
                session_id=session_id,
                run_id=onto_run_id or "(empty)",
                trace_id=best.get("id"),
                diff_seconds=best_diff,
            )
        return best

    # 并发 drain：锁内过滤 claimed + 选择 + claim，保证原子性
    if claimed_lock is not None and claimed_trace_ids is not None:
        async with claimed_lock:
            # 过滤已被其他 job 占用的 trace
            available = [t for t in batch if (t.get("id") or "") not in claimed_trace_ids]
            if not available:
                return None
            selected = _select(available)
            if selected:
                _rid = (selected.get("id") or "").strip()
                if _rid:
                    claimed_trace_ids.add(_rid)
                log(
                    "INFO",
                    "TRACE_SYNC",
                    "Level 2 session_id + run_id 精确命中",
                    session_id=session_id,
                    run_id=onto_run_id or "(empty)",
                    trace_id=_rid,
                )
            return selected
    else:
        # 无并发，直接选择
        selected = _select(batch)
        if selected and onto_run_id:
            _rid = (selected.get("id") or "").strip()
            if _rid:
                log(
                    "INFO",
                    "TRACE_SYNC",
                    "Level 2 session_id + run_id 精确命中",
                    session_id=session_id,
                    run_id=onto_run_id,
                    trace_id=_rid,
                )
        return selected


async def process_trace_sync_job(
    session: AsyncSession,
    job: TraceSyncJob,
    *,
    claimed_trace_ids: Optional[Set[str]] = None,
    claimed_lock: Optional[asyncio.Lock] = None,
) -> Dict[str, Any]:
    """处理单条 SyncJob；失败可重试直至 MAX_ATTEMPTS。

    三级递进查找：
      Level 1: get_trace(trace_id) 直查
      Level 2: list_traces(session_id) + run_id 精确匹配
      Level 3: find_trace_by_time_window 时间窗 fallback（兜底）
    """
    now = datetime.utcnow()

    # 落库延迟容忍：首次处理前等待 MIN_INGESTION_DELAY 秒
    if job.attempts == 0 and TRACE_SYNC_MIN_INGESTION_DELAY_SEC > 0:
        age = (now - job.created_at).total_seconds()
        if age < TRACE_SYNC_MIN_INGESTION_DELAY_SEC:
            # 还太早，跳过本轮，保持 pending 等下次轮询
            log(
                "DEBUG",
                "TRACE_SYNC",
                "Job 尚未到落库延迟窗口，跳过本轮",
                job_id=job.id,
                age_sec=int(age),
                min_delay=TRACE_SYNC_MIN_INGESTION_DELAY_SEC,
            )
            return {"job_id": job.id, "status": "deferred"}

    job.status = "running"
    job.attempts = (job.attempts or 0) + 1
    await session.flush()

    run = await get_agent_run(session, job.agent_run_id)
    if not run:
        job.status = "failed"
        job.last_error = "agent_run 不存在"
        job.failure_category = "agent_run_missing"
        job.processed_at = now
        await session.flush()
        return {"job_id": job.id, "status": job.status, "error": job.last_error}

    if not (job.trace_id or "").strip():
        job.status = "skipped"
        job.processed_at = now
        run.trace_sync_status = "skipped"
        await session.flush()
        return {"job_id": job.id, "status": job.status}

    if not LANGFUSE_ENABLED:
        job.status = "failed"
        job.last_error = "Langfuse 未配置"
        job.failure_category = "config_error"
        job.processed_at = now
        run.trace_sync_status = "failed"
        await session.flush()
        return {"job_id": job.id, "status": job.status, "error": job.last_error}

    try:
        try:
            client = get_langfuse_client(job.langfuse_project)
        except ProjectNotFoundError as e:
            # 项目未注册：重试无意义，直接标记 failed
            job.status = "failed"
            job.last_error = str(e)
            job.failure_category = "config_error"
            job.processed_at = now
            run.trace_sync_status = "failed"
            await session.flush()
            log(
                "ERROR",
                "TRACE_SYNC",
                "Langfuse 项目未注册，直接标记 failed",
                job_id=job.id,
                project=job.langfuse_project,
                error=str(e),
            )
            return {"job_id": job.id, "status": job.status, "error": str(e)}

        # 查询同 task 其他 run 已占用的 trace_id，加入 claimed_trace_ids。
        # claimed_trace_ids 只在单次 drain 周期内有效，跨 drain 周期不共享，
        # 导致并行多轮推理（started_at 高度重叠、session_id 缺失）时
        # Level 2/3 时间窗 fallback 在不同 drain 周期重复选中同一条 trace。
        # 用 DB 做全局去重，保证一条 trace 只分配给一个 run。
        db_claimed: Set[str] = set()
        if run.task_id:
            db_claimed_result = await session.execute(
                select(AgentRun.trace_id).where(
                    AgentRun.trace_id.isnot(None),
                    AgentRun.trace_id != "",
                    AgentRun.task_id == run.task_id,
                    AgentRun.id != run.id,
                )
            )
            db_claimed = {
                tid for tid in db_claimed_result.scalars() if tid
            }
            if db_claimed and claimed_trace_ids is not None:
                if claimed_lock is not None:
                    async with claimed_lock:
                        claimed_trace_ids.update(db_claimed)
                else:
                    claimed_trace_ids.update(db_claimed)
                log(
                    "INFO",
                    "TRACE_SYNC",
                    "已将同 task 其他 run 的 trace_id 加入 claimed（跨 drain 去重）",
                    job_id=job.id,
                    task_id=run.task_id,
                    db_claimed_count=len(db_claimed),
                )

        trace = None

        # ── Level 1: trace_id 直查 ──
        # 若 job.trace_id 已被同 task 其他 run 占用（跨 drain 回写错误），
        # 跳过 Level 1，降级到 Level 2/3 重新定位正确的 trace。
        try:
            if (job.trace_id or "").strip() in db_claimed:
                raise RuntimeError(
                    f"trace_id {job.trace_id} 已被同 task 其他 run 占用"
                )
            trace = await client.get_trace(job.trace_id, timeout=LANGFUSE_WORKER_TIMEOUT)
        except Exception as primary_err:
            primary_err_str = str(primary_err)[:300]
            log(
                "WARN",
                "TRACE_SYNC",
                "Level 1 trace_id 直查失败",
                job_id=job.id,
                trace_id=job.trace_id,
                error=primary_err_str,
            )

            # ── Level 2: session_id + run_id 精确查找 ──
            trace = await _find_trace_by_session(
                client, run,
                timeout=LANGFUSE_WORKER_TIMEOUT,
                claimed_trace_ids=claimed_trace_ids,
                claimed_lock=claimed_lock,
            )

            # ── Level 3: 时间窗 fallback（兜底）──
            if not trace:
                log(
                    "INFO",
                    "TRACE_SYNC",
                    "Level 2 未命中，降级到 Level 3 时间窗 fallback",
                    job_id=job.id,
                    trace_id=job.trace_id,
                )
                session_id = (getattr(run, "onto_agent_session_id", None) or "").strip() or None
                # 并发 drain 时用锁保护 claimed_trace_ids 的读+写原子性
                if claimed_lock is not None:
                    async with claimed_lock:
                        trace = await find_trace_by_time_window(
                            client=client,
                            anchor_ts=run.started_at,
                            project_label=job.langfuse_project,
                            timeout=LANGFUSE_WORKER_TIMEOUT,
                            session_id=session_id,
                            claimed_trace_ids=claimed_trace_ids,
                            end_ts=getattr(run, "ended_at", None),
                        )
                        # 命中后立即 claim，防止其他 job 误匹配同一条 trace
                        if trace and claimed_trace_ids is not None:
                            _rid = (trace.get("id") or "").strip()
                            if _rid:
                                claimed_trace_ids.add(_rid)
                else:
                    trace = await find_trace_by_time_window(
                        client=client,
                        anchor_ts=run.started_at,
                        project_label=job.langfuse_project,
                        timeout=LANGFUSE_WORKER_TIMEOUT,
                        session_id=session_id,
                        claimed_trace_ids=claimed_trace_ids,
                        end_ts=getattr(run, "ended_at", None),
                    )

            if not trace:
                raise primary_err

            # 命中后回写真实 trace_id（Level 2/3 命中后，下次直查即可）
            resolved_id = (trace.get("id") or "").strip()
            if resolved_id and resolved_id != (job.trace_id or "").strip():
                old_id = job.trace_id
                job.trace_id = resolved_id
                run.trace_id = resolved_id
                log(
                    "INFO",
                    "TRACE_SYNC",
                    "fallback 命中，回写 trace_id",
                    job_id=job.id,
                    old_trace_id=old_id,
                    new_trace_id=resolved_id,
                )

            # 标记已占用，避免同批其他 run 重复匹配
            if claimed_trace_ids is not None and resolved_id:
                if claimed_lock is not None:
                    async with claimed_lock:
                        claimed_trace_ids.add(resolved_id)
                else:
                    claimed_trace_ids.add(resolved_id)

            # 拿到 trace 全量数据（含 observations / scores）
            try:
                full = await client.get_trace(resolved_id, timeout=LANGFUSE_WORKER_TIMEOUT)
                if full:
                    trace = full
            except Exception as e:
                log(
                    "WARN",
                    "TRACE_SYNC",
                    "fallback 后 get_trace 失败，使用 list 拿到的 best",
                    trace_id=resolved_id,
                    error=str(e),
                )

        # ── Observation 稳定性检查 ──
        # onto-agent 异步上传 observations，首次拉取可能只有 root span。
        # 通过连续拉取对比 obs count，确认上传完成后才处理。
        should_defer, stability_reason, stability_metadata = _check_observation_stability(
            job, trace, now
        )
        if should_defer:
            # 撤销 attempts 递增：稳定性检查不消耗错误重试预算
            job.attempts = max(0, (job.attempts or 0) - 1)
            job.status = "pending"
            job.failure_category = "incomplete_trace"
            job.last_error = f"{_STABILITY_PREFIX}{json.dumps(stability_metadata, separators=(',', ':'))}"
            job.processed_at = None
            run.trace_sync_status = "pending_sync"
            await session.flush()
            log(
                "DEBUG",
                "TRACE_SYNC",
                "观测数稳定性检查 deferred",
                job_id=job.id,
                trace_id=job.trace_id,
                reason=stability_reason,
            )
            return {"job_id": job.id, "status": "deferred_incomplete", "reason": stability_reason}

        facts = await build_facts_for_trace(session, run, trace)

        # ── 物化不完整校验 ──
        # span_nodes=0 但 trace.observations 非空 → onto-agent 异步上传未完成
        # 即时重拉一次，仍不完整则走 defer 重试（独立计数 rerun_count）
        incomplete = facts.get("incomplete_materialization")
        if incomplete:
            try:
                re_trace = await client.get_trace(job.trace_id, timeout=LANGFUSE_WORKER_TIMEOUT)
                if re_trace and count_observations(re_trace) > (facts.get("observation_count") or 0):
                    facts = await build_facts_for_trace(session, run, re_trace)
                    incomplete = facts.get("incomplete_materialization")
            except Exception as re_err:
                log(
                    "WARN",
                    "TRACE_SYNC",
                    "物化不完整即时重拉失败，走 defer",
                    job_id=job.id,
                    trace_id=job.trace_id,
                    error=str(re_err)[:300],
                )

        if incomplete:
            return await _defer_incomplete_materialization(session, job, run, now, incomplete)

        job.status = "success"
        job.processed_at = now
        job.last_error = None
        job.failure_category = None
        run.trace_sync_status = "synced"
        await session.flush()
        return {"job_id": job.id, "status": job.status, **facts}
    except Exception as e:
        err = str(e)[:2000]
        job.last_error = err
        job.failure_category = classify_failure(e)
        job.processed_at = now
        if job.attempts >= TRACE_SYNC_MAX_ATTEMPTS:
            job.status = "failed"
            run.trace_sync_status = "failed"
            log(
                "ERROR",
                "TRACE_SYNC",
                "Job 达到最大重试次数，标记 failed",
                job_id=job.id,
                trace_id=job.trace_id,
                attempts=job.attempts,
                max_attempts=TRACE_SYNC_MAX_ATTEMPTS,
                error=err,
            )
        else:
            job.status = "pending"
            run.trace_sync_status = "pending_sync"
            log(
                "WARNING",
                "TRACE_SYNC",
                "Job 处理失败，稍后重试",
                job_id=job.id,
                trace_id=job.trace_id,
                attempts=job.attempts,
                max_attempts=TRACE_SYNC_MAX_ATTEMPTS,
                error=err,
            )
        await session.flush()
        return {"job_id": job.id, "status": job.status, "error": err}


async def _defer_incomplete_materialization(
    session: AsyncSession,
    job: TraceSyncJob,
    run: AgentRun,
    now: datetime,
    incomplete: Dict[str, Any],
) -> Dict[str, Any]:
    """物化不完整（span_nodes=0 但 API 有 observations）→ 独立计数重试。

    rerun_count 独立于 attempts（网络错误预算），专门用于"API 有数据但物化为空"场景：
    - 未达上限：重置 job 为 pending，等待下轮 drain 重试
    - 达上限：标记 synced_incomplete 终态，前端可见，用户可手动 retry
    """
    job.rerun_count = (job.rerun_count or 0) + 1
    job.incomplete_metadata = json.dumps(incomplete, ensure_ascii=False)
    job.failure_category = "incomplete_materialization"

    if job.rerun_count >= TRACE_SYNC_MAX_RERUN:
        job.status = "success"
        job.processed_at = now
        run.trace_sync_status = "synced_incomplete"
        await session.flush()
        log(
            "WARN",
            "TRACE_SYNC",
            "物化不完整达上限，标记 synced_incomplete",
            job_id=job.id,
            agent_run_id=run.id,
            trace_id=job.trace_id,
            rerun_count=job.rerun_count,
            max_rerun=TRACE_SYNC_MAX_RERUN,
            incomplete=incomplete,
        )
        return {
            "job_id": job.id,
            "status": "synced_incomplete",
            "rerun_count": job.rerun_count,
        }

    # 仍可重试 → 重新入 pending 队列，等下轮 drain
    job.status = "pending"
    job.processed_at = None
    job.last_error = f"INCOMPLETE_MATERIALIZATION:{json.dumps(incomplete, separators=(',', ':'))}"
    run.trace_sync_status = "pending_sync"
    await session.flush()
    log(
        "INFO",
        "TRACE_SYNC",
        "物化不完整，defer 等待重试",
        job_id=job.id,
        agent_run_id=run.id,
        trace_id=job.trace_id,
        rerun_count=job.rerun_count,
        max_rerun=TRACE_SYNC_MAX_RERUN,
        incomplete=incomplete,
    )
    return {
        "job_id": job.id,
        "status": "deferred_incomplete",
        "rerun_count": job.rerun_count,
    }


async def _sweep_incomplete_materialization() -> int:
    """扫描 synced_incomplete 的 run，重新入队等待 drain 处理。

    限频 TRACE_SYNC_SWEEP_INTERVAL_SEC（默认 60s），避免每轮 drain 都扫全表。
    只扫最近 TRACE_SYNC_INCOMPLETE_LOOKBACK_HOURS 小时内的 run，避免历史脏数据。
    跳过已有 pending/running job 的 run，防重复处理。
    """
    global _last_sweep_at
    now_ts = time.time()
    if now_ts - _last_sweep_at < TRACE_SYNC_SWEEP_INTERVAL_SEC:
        return 0
    _last_sweep_at = now_ts

    cutoff = datetime.utcnow() - timedelta(hours=TRACE_SYNC_INCOMPLETE_LOOKBACK_HOURS)
    requeued = 0
    try:
        async with async_session() as session:
            runs_result = await session.execute(
                select(AgentRun).where(
                    AgentRun.trace_sync_status == "synced_incomplete",
                    AgentRun.started_at >= cutoff,
                ).limit(TRACE_SYNC_SWEEP_BATCH_SIZE)
            )
            runs = list(runs_result.scalars().all())
            if not runs:
                return 0

            for run in runs:
                # 跳过已有 pending/running job 的 run
                active = await session.execute(
                    select(TraceSyncJob.id).where(
                        TraceSyncJob.agent_run_id == run.id,
                        TraceSyncJob.status.in_(["pending", "running"]),
                    ).limit(1)
                )
                if active.scalar_one_or_none():
                    continue

                # 找原 success + incomplete_materialization job，重置为 pending
                job_row = await session.execute(
                    select(TraceSyncJob).where(
                        TraceSyncJob.agent_run_id == run.id,
                        TraceSyncJob.status == "success",
                        TraceSyncJob.failure_category == "incomplete_materialization",
                    ).order_by(TraceSyncJob.processed_at.desc()).limit(1)
                )
                job = job_row.scalar_one_or_none()
                if not job:
                    continue
                job.status = "pending"
                job.processed_at = None
                run.trace_sync_status = "pending_sync"
                requeued += 1

            if requeued > 0:
                await session.commit()
                log(
                    "INFO",
                    "TRACE_SYNC_SWEEP",
                    "补扫 synced_incomplete 重新入队",
                    requeued=requeued,
                    scanned=len(runs),
                )
    except Exception as e:
        log(
            "ERROR",
            "TRACE_SYNC_SWEEP",
            "补扫 synced_incomplete 异常",
            error=str(e)[:500],
        )
    return requeued


async def drain_pending_trace_sync_jobs(*, limit: int = 20) -> int:
    """拉取并处理 pending 任务，返回成功处理条数（success + skipped）。

    并发处理：每 job 独立 session，_TRACE_SYNC_SEMAPHORE(5) 控制并发，
    claimed_lock 保护 claimed_trace_ids 的读写原子性。
    """
    claimed_trace_ids: Set[str] = set()
    claimed_lock = asyncio.Lock()

    # 先用短生命周期 session 拉取待处理 job ID 列表
    async with async_session() as session:
        jobs = await _fetch_pending_jobs(session, limit)
        job_ids = [j.id for j in jobs]

    if not job_ids:
        return 0

    async def _process_one(job_id: str) -> Dict[str, Any]:
        """单 job 并发处理：独立 session + 信号量控制。"""
        async with _TRACE_SYNC_SEMAPHORE, async_session() as job_session:
            # 在当前 session 中重新查询 job，避免跨 session 的 detached 对象问题
            result = await job_session.execute(
                select(TraceSyncJob).where(TraceSyncJob.id == job_id)
            )
            job = result.scalar_one_or_none()
            if not job:
                return {"job_id": job_id, "status": "skipped"}
            result = await process_trace_sync_job(
                job_session,
                job,
                claimed_trace_ids=claimed_trace_ids,
                claimed_lock=claimed_lock,
            )
            await job_session.commit()
            return result

    results = await asyncio.gather(
        *[_process_one(jid) for jid in job_ids],
        return_exceptions=True,
    )

    processed = 0
    for r in results:
        if isinstance(r, Exception):
            log(
                "ERROR",
                "TRACE_SYNC_DRAIN",
                "并发处理 job 异常",
                error=str(r)[:500],
            )
            continue
        if r.get("status") in ("success", "skipped"):
            processed += 1

    async with async_session() as session:
        stats = await get_trace_sync_queue_stats(session)
        if processed > 0:
            log(
                "INFO",
                "TRACE_SYNC_DRAIN",
                "队列 drain 完成",
                processed=processed,
                limit=limit,
                **stats.as_dict(),
            )
        else:
            log(
                "DEBUG",
                "TRACE_SYNC_DRAIN",
                "队列 drain 完成（无待处理）",
                processed=0,
                **stats.as_dict(),
            )

    # 补扫 synced_incomplete 的 run，重新入队等待下轮 drain
    await _sweep_incomplete_materialization()

    return processed
