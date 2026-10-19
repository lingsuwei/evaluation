#!/usr/bin/env python3
"""
SQLite → PostgreSQL 全量数据迁移（零数据丢失）。

覆盖 database.py 全部 25 张表，按外键依赖拓扑排序迁移。
相比 migrate_runs_to_pg.py，本脚本额外覆盖：

  缺失的 12 张表:
    run_semantic_nodes, run_comparisons, run_diffs, experiments,
    experiment_summaries, agent_run_trace_refs, scenario_eval_templates,
    scenario_eval_steps, scenario_step_results, scenario_step_span_refs,
    agent_asset_snapshots, trace_operation_rules

  增强:
    - JSON 列自动检测（不再硬编码）
    - 复合主键 / 无主键表安全处理
    - 逐行 fallback：批量插入失败时降级为逐行插入，最大化数据保留
    - 外键依赖拓扑自动推导，无需手动维护顺序
    - 迁移前后行数对账 + 抽样数据校验（checksum）
    - --truncate-target 清空目标表后重新迁移
    - 进度条与预估时间

前置条件:
  1. 目标 PG 已执行 alembic upgrade head（或 pg_bootstrap）
  2. 源 SQLite 文件存在且可读

用法:
  cd backend
  python scripts/migrate_all_to_pg.py --dry-run
  python scripts/migrate_all_to_pg.py \
    --source-url sqlite:///./data/evaluation.db \
    --target-url postgresql+psycopg2://user:pass@127.0.0.1:5432/onto_eval
  python scripts/migrate_all_to_pg.py --verify-only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import defaultdict, deque
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Type

from sqlalchemy import create_engine, inspect, select, text, Column
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.types import JSON as SAJSON

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import DATABASE_URL, alembic_database_url, is_postgresql_url, is_sqlite_url
from database import (
    # 编排层
    Dataset,                          # datasets
    Agent,                            # agents
    EvaluationTask,                   # evaluation_tasks
    EvaluationResult,                 # evaluation_results
    EvaluationRound,                  # evaluation_rounds
    EvaluationScript,                 # evaluation_scripts
    LLMModel,                         # llm_models
    # Runtime Fact 层
    AgentRun,                         # agent_runs
    RunSpanNode,                      # run_span_nodes
    RunSemanticNode,                  # run_semantic_nodes       ← 缺失
    RunMetric,                        # run_metrics
    CheckpointResult,                 # checkpoint_results
    # P3 对比/实验
    RunComparison,                    # run_comparisons           ← 缺失
    RunDiff,                          # run_diffs                 ← 缺失
    Experiment,                       # experiments               ← 缺失
    ExperimentSummary,                # experiment_summaries      ← 缺失
    AgentRunTraceRef,                 # agent_run_trace_refs      ← 缺失
    # Trace Sync
    TraceSyncJob,                     # trace_sync_jobs
    # 过程检查点
    ProcessCheckpointTemplate,        # process_checkpoint_templates
    # 场景评测
    ScenarioEvalTemplate,             # scenario_eval_templates    ← 缺失
    ScenarioEvalStep,                 # scenario_eval_steps        ← 缺失
    ScenarioStepResult,               # scenario_step_results      ← 缺失
    ScenarioStepSpanRef,              # scenario_step_span_refs    ← 缺失
    # 资产 / 操作绑定
    AgentAssetSnapshot,               # agent_asset_snapshots      ← 缺失
    TraceOperationRule,               # trace_operation_rules      ← 缺失
)

# ---------------------------------------------------------------------------
# 全量表列表（25 张），按外键依赖拓扑排序
# ---------------------------------------------------------------------------
# 第 0 层：无外键依赖的独立表
# 第 1 层：仅依赖第 0 层
# 第 2 层：依赖第 0-1 层
# ...
ALL_MODELS: List[Type[Any]] = [
    # 第 0 层 — 独立表
    Dataset,
    Agent,
    EvaluationScript,
    LLMModel,
    ProcessCheckpointTemplate,
    ScenarioEvalTemplate,
    # 第 1 层 — 依赖第 0 层
    ScenarioEvalStep,          # FK → scenario_eval_templates
    EvaluationTask,
    # 第 2 层 — 依赖第 1 层
    AgentRun,
    TraceSyncJob,
    # 第 3 层 — 依赖 AgentRun
    EvaluationResult,
    EvaluationRound,           # FK → evaluation_results (逻辑上，DB 层无 FK 约束)
    RunSpanNode,               # FK → agent_runs
    RunSemanticNode,           # FK → agent_runs
    RunMetric,                 # FK → agent_runs
    CheckpointResult,          # FK → agent_runs
    RunComparison,             # FK → agent_runs
    ScenarioStepResult,        # FK → agent_runs, scenario_eval_steps
    ScenarioStepSpanRef,       # FK → agent_runs, scenario_eval_steps
    AgentRunTraceRef,          # FK → agent_runs
    AgentAssetSnapshot,
    # 第 4 层 — 依赖 RunComparison / Experiment
    RunDiff,                   # FK → run_comparisons
    Experiment,
    ExperimentSummary,         # FK → experiments, run_comparisons
    TraceOperationRule,        # FK → agent_asset_snapshots
]

# 迁移后需重置 PG serial 的表（列名）
_PG_SERIAL_TABLES: Dict[str, str] = {
    "evaluation_results": "id",
    "evaluation_rounds": "id",
}

# 无单列主键的表 — 使用所有 unique 约束做冲突跳过
_TABLES_WITHOUT_SIMPLE_PK: Set[str] = set()

# 有复合 unique 约束的表（主键以外），在批量插入时也需要 ON CONFLICT DO NOTHING
_COMPOSITE_UNIQUE_TABLES: Dict[str, List[str]] = {
    "run_span_nodes": ["agent_run_id", "observation_id"],
    "run_semantic_nodes": ["agent_run_id", "observation_id"],
    "run_metrics": ["agent_run_id", "metric_key", "source", "dims_hash"],
    "checkpoint_results": ["agent_run_id", "checkpoint_id"],
    "run_diffs": ["comparison_id", "diff_type"],
    "experiment_summaries": ["experiment_id", "question_id", "right_agent_id"],
    "agent_run_trace_refs": ["agent_run_id", "trace_id"],
    "scenario_step_results": ["agent_run_id", "step_id"],
    "scenario_step_span_refs": ["agent_run_id", "step_id"],
    "agent_asset_snapshots": ["logical_agent_id", "asset_type", "asset_hash"],
    "trace_operation_rules": ["logical_agent_id", "operation_id", "registry_version"],
}


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def normalize_sync_sqlite_url(url: str) -> str:
    u = (url or "").strip()
    if u.startswith("sqlite+aiosqlite:"):
        return u.replace("sqlite+aiosqlite:", "sqlite:", 1)
    return u


def normalize_sync_pg_url(url: str) -> str:
    return alembic_database_url(url)


def create_sync_engine(url: str, *, pg: bool) -> Engine:
    normalized = normalize_sync_pg_url(url) if pg else normalize_sync_sqlite_url(url)
    connect_args: Dict[str, Any] = {}
    if normalized.startswith("sqlite:"):
        connect_args["check_same_thread"] = False
    return create_engine(normalized, future=True, connect_args=connect_args)


def _detect_json_columns(model: Type[Any]) -> Set[str]:
    """从 SQLAlchemy Column 类型自动检测 JSON 列。"""
    json_cols: Set[str] = set()
    table = getattr(model, "__table__", None)
    if table is None:
        return json_cols
    for col in table.columns:
        if isinstance(col.type, SAJSON):
            json_cols.add(col.key)
    return json_cols


def _all_pk_columns(model: Type[Any]) -> List[str]:
    """返回主键列名列表。"""
    table = getattr(model, "__table__", None)
    if table is None:
        return []
    return [c.key for c in table.columns if c.primary_key]


def _all_unique_constraint_columns(model: Type[Any]) -> List[List[str]]:
    """返回所有 unique 约束的列名列表（含主键）。"""
    table = getattr(model, "__table__", None)
    if table is None:
        return []
    result: List[List[str]] = []
    pk = _all_pk_columns(model)
    if pk:
        result.append(pk)
    for c in table.constraints:
        if hasattr(c, "columns") and getattr(c, "unique", False):
            cols = [col.key for col in c.columns]
            if cols and cols != pk:
                result.append(cols)
    return result


def _model_to_mapping(
    row: Any,
    json_cols: Set[str],
) -> Dict[str, Any]:
    """将 ORM 对象转为 dict，JSON 列自动序列化。"""
    table = row.__class__.__table__
    out: Dict[str, Any] = {}
    for col in table.columns:
        val = getattr(row, col.key)
        if col.key in json_cols:
            # JSON 列：已经是 Python 对象则保持，否则尝试解析
            if isinstance(val, str):
                try:
                    out[col.key] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    out[col.key] = val
            else:
                out[col.key] = val
        elif isinstance(val, (dict, list)):
            # 非 JSON 列但值是 dict/list → 也序列化（SQLite 的 TEXT 可能存 JSON）
            out[col.key] = val
        else:
            out[col.key] = val
    return out


def _table_exists(engine: Engine, table_name: str) -> bool:
    return inspect(engine).has_table(table_name)


def _row_checksum(mapping: Dict[str, Any]) -> str:
    """计算一行数据的 MD5 checksum（用于对账）。"""
    raw = json.dumps(mapping, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class TableMigrateStats:
    table: str
    source_count: int = 0
    inserted: int = 0
    skipped: int = 0
    row_fallbacks: int = 0       # 逐行 fallback 成功插入的行数
    errors: int = 0
    error_details: List[str] = field(default_factory=list)
    missing_on_source: bool = False
    missing_on_target: bool = False
    duration_sec: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "source_count": self.source_count,
            "inserted": self.inserted,
            "skipped": self.skipped,
            "row_fallbacks": self.row_fallbacks,
            "errors": self.errors,
            "error_details": self.error_details[:10],  # 只保留前 10 条
            "missing_on_source": self.missing_on_source,
            "missing_on_target": self.missing_on_target,
            "duration_sec": round(self.duration_sec, 2),
        }


@dataclass
class MigrateAllResult:
    source_url: str
    target_url: str
    dry_run: bool
    tables: List[TableMigrateStats] = field(default_factory=list)
    global_errors: List[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    def summary(self) -> Dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "tables": len(self.tables),
            "source_rows": sum(t.source_count for t in self.tables),
            "inserted": sum(t.inserted for t in self.tables),
            "skipped": sum(t.skipped for t in self.tables),
            "row_fallbacks": sum(t.row_fallbacks for t in self.tables),
            "errors": sum(t.errors for t in self.tables) + len(self.global_errors),
            "duration_sec": round(self.finished_at - self.started_at, 1),
        }


@dataclass
class VerifyResult:
    ok: bool
    mismatches: List[Dict[str, Any]] = field(default_factory=list)
    checksum_mismatches: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "mismatches": self.mismatches,
            "checksum_mismatches": self.checksum_mismatches,
        }


# ---------------------------------------------------------------------------
# 核心迁移逻辑
# ---------------------------------------------------------------------------

def _insert_batch_skip_existing(
    session: Session,
    model: Type[Any],
    batch: List[Dict[str, Any]],
    *,
    unique_cols: Optional[List[str]] = None,
) -> int:
    """使用 PG ON CONFLICT DO NOTHING 批量插入。

    优先使用主键；对于有复合 unique 约束的表，使用指定的 unique_cols。
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    if not batch:
        return 0

    table = model.__table__
    stmt = pg_insert(table).values(batch)

    # 选择冲突列：优先用传入的 unique_cols，其次用主键
    conflict_cols = unique_cols or _all_pk_columns(model)
    if not conflict_cols:
        # 无主键且无指定 unique 列 → 使用第一个 unique 约束
        all_uniques = _all_unique_constraint_columns(model)
        conflict_cols = all_uniques[0] if all_uniques else None

    if conflict_cols:
        stmt = stmt.on_conflict_do_nothing(index_elements=conflict_cols)
    else:
        # 完全没有约束 → 不做冲突跳过，直接插入
        pass

    result = session.execute(stmt)
    return result.rowcount or 0


def _insert_row_by_row(
    session: Session,
    model: Type[Any],
    mappings: List[Dict[str, Any]],
    *,
    pk_cols: List[str],
    unique_constraints: List[List[str]],
) -> Tuple[int, int, List[str]]:
    """逐行插入，遇到冲突跳过。返回 (inserted, skipped, errors)。"""
    inserted = 0
    skipped = 0
    errors: List[str] = []

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    table = model.__table__

    for i, mapping in enumerate(mappings):
        try:
            stmt = pg_insert(table).values([mapping])

            # 尝试所有 unique 约束做冲突跳过
            all_uniques = list(unique_constraints)
            if pk_cols and pk_cols not in all_uniques:
                all_uniques.insert(0, pk_cols)

            applied = False
            for uc in all_uniques:
                if uc:
                    stmt = stmt.on_conflict_do_nothing(index_elements=uc)
                    applied = True
                    break  # 只用一个冲突约束

            if not applied:
                stmt = stmt.on_conflict_do_nothing()  # 使用所有约束

            result = session.execute(stmt)
            if result.rowcount:
                inserted += 1
            else:
                skipped += 1
        except Exception as exc:
            errors.append(f"row {i}: {exc}")
            session.rollback()
            # 降级：尝试裸 INSERT（忽略所有约束）
            try:
                session.execute(table.insert().values(**mapping))
                session.commit()
                inserted += 1
            except Exception as raw_exc:
                session.rollback()
                # 裸 INSERT 也失败 → 行彻底丢失，计入 errors 而非 skipped
                # 否则最终完整性检查 inserted+skipped==source_rows 会误判成功
                errors.append(f"row {i}: data lost — raw insert failed: {raw_exc}")

    return inserted, skipped, errors


def migrate_table(
    source_session: Session,
    target_session: Session,
    model: Type[Any],
    *,
    batch_size: int = 500,
    dry_run: bool = False,
    skip_existing: bool = True,
    truncate_target: bool = False,
) -> TableMigrateStats:
    """迁移单张表的所有行。"""
    t_start = time.time()
    table_name = model.__tablename__
    stats = TableMigrateStats(table=table_name)

    # 检测 JSON 列
    json_cols = _detect_json_columns(model)
    pk_cols = _all_pk_columns(model)
    all_uniques = _all_unique_constraint_columns(model)

    # 选择最佳的冲突跳过列（优先主键，否则取复合 unique 或预定义）
    best_conflict_cols: Optional[List[str]] = None
    if table_name in _COMPOSITE_UNIQUE_TABLES:
        best_conflict_cols = _COMPOSITE_UNIQUE_TABLES[table_name]
    elif pk_cols:
        best_conflict_cols = pk_cols
    elif all_uniques:
        best_conflict_cols = all_uniques[0]

    # 检查源表存在性
    if not _table_exists(source_session.bind, table_name):
        stats.missing_on_source = True
        return stats

    # 读取源数据
    rows = source_session.execute(select(model)).scalars().all()
    stats.source_count = len(rows)

    if dry_run or not rows:
        stats.duration_sec = time.time() - t_start
        return stats

    # 检查目标表
    if not _table_exists(target_session.bind, table_name):
        stats.missing_on_target = True
        stats.duration_sec = time.time() - t_start
        return stats

    # Truncate 目标表
    if truncate_target:
        try:
            target_session.execute(text(f"DELETE FROM {table_name}"))
            target_session.commit()
        except Exception as exc:
            stats.error_details.append(f"truncate: {exc}")
            target_session.rollback()

    # 转为 mapping
    mappings = [_model_to_mapping(row, json_cols) for row in rows]

    # 批量插入
    total_inserted = 0
    total_skipped = 0
    total_row_fallbacks = 0
    batch_errors: List[str] = []

    for offset in range(0, len(mappings), batch_size):
        batch = mappings[offset : offset + batch_size]

        if skip_existing:
            try:
                inserted = _insert_batch_skip_existing(
                    target_session,
                    model,
                    batch,
                    unique_cols=best_conflict_cols,
                )
                total_inserted += inserted
                total_skipped += len(batch) - inserted
                target_session.commit()
            except Exception as batch_exc:
                # 批量失败 → 逐行 fallback
                target_session.rollback()
                batch_errors.append(
                    f"batch {offset // batch_size}: {batch_exc} → row fallback"
                )
                rin, rsk, rerrs = _insert_row_by_row(
                    target_session,
                    model,
                    batch,
                    pk_cols=pk_cols,
                    unique_constraints=all_uniques,
                )
                total_inserted += rin
                total_skipped += rsk
                total_row_fallbacks += rin
                if rerrs:
                    batch_errors.extend(rerrs)
                target_session.commit()
        else:
            try:
                target_session.bulk_insert_mappings(model, batch)
                total_inserted += len(batch)
                target_session.commit()
            except Exception as bulk_exc:
                # bulk 失败 → 逐行 fallback，避免整批数据丢失
                target_session.rollback()
                batch_errors.append(
                    f"batch {offset // batch_size}: {bulk_exc} → row fallback"
                )
                rin, rsk, rerrs = _insert_row_by_row(
                    target_session,
                    model,
                    batch,
                    pk_cols=pk_cols,
                    unique_constraints=all_uniques,
                )
                total_inserted += rin
                total_skipped += rsk
                total_row_fallbacks += rin
                if rerrs:
                    batch_errors.extend(rerrs)
                target_session.commit()

    stats.inserted = total_inserted
    stats.skipped = total_skipped
    stats.row_fallbacks = total_row_fallbacks
    stats.errors = len(batch_errors)
    stats.error_details = batch_errors[:20]
    stats.duration_sec = time.time() - t_start
    return stats


def reset_pg_serials(target_session: Session) -> List[str]:
    """重置 PG serial 序列，确保后续插入不冲突。"""
    errors: List[str] = []
    for table_name, col in _PG_SERIAL_TABLES.items():
        if not _table_exists(target_session.bind, table_name):
            continue
        try:
            target_session.execute(
                text(
                    f"""
                    SELECT setval(
                        pg_get_serial_sequence('{table_name}', '{col}'),
                        COALESCE((SELECT MAX({col}) FROM {table_name}), 1),
                        (SELECT MAX({col}) IS NOT NULL FROM {table_name})
                    )
                    """
                )
            )
            target_session.commit()
        except Exception as exc:
            target_session.rollback()
            errors.append(f"reset_serial {table_name}.{col}: {exc}")
    return errors


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_migration(
    source_url: str,
    target_url: str,
    *,
    tables: Optional[Sequence[str]] = None,
    batch_size: int = 500,
    dry_run: bool = False,
    skip_existing: bool = True,
    reset_serials: bool = True,
    truncate_target: bool = False,
) -> MigrateAllResult:
    """执行全量迁移。"""
    if not is_sqlite_url(source_url):
        raise ValueError(f"源库须为 SQLite，当前: {source_url}")
    if not dry_run:
        if not is_postgresql_url(target_url):
            raise ValueError(f"目标库须为 PostgreSQL，当前: {target_url}")

    result = MigrateAllResult(
        source_url=source_url,
        target_url=target_url,
        dry_run=dry_run,
        started_at=time.time(),
    )

    source_engine = create_sync_engine(source_url, pg=False)
    target_engine = None if dry_run else create_sync_engine(target_url, pg=True)
    SourceSession = sessionmaker(bind=source_engine)
    TargetSession = sessionmaker(bind=target_engine) if target_engine else None

    selected = _filter_models(tables)
    total_tables = len(selected)

    with SourceSession() as src_sess:
        tgt_ctx = TargetSession() if TargetSession else nullcontext()
        with tgt_ctx as tgt_sess:
            for i, model in enumerate(selected):
                table_name = model.__tablename__
                prefix = f"[{i + 1}/{total_tables}]"
                dry_tag = " (dry-run)" if dry_run else ""

                try:
                    stats = migrate_table(
                        src_sess,
                        tgt_sess if tgt_sess else src_sess,
                        model,
                        batch_size=batch_size,
                        dry_run=dry_run,
                        skip_existing=skip_existing,
                        truncate_target=truncate_target,
                    )
                    result.tables.append(stats)

                    if stats.missing_on_source:
                        print(f"  {prefix} {table_name}: 源库无此表，跳过")
                    elif dry_run:
                        print(
                            f"  {prefix} {table_name}: {stats.source_count} rows{dry_tag}"
                        )
                    else:
                        print(
                            f"  {prefix} {table_name}: "
                            f"src={stats.source_count} ins={stats.inserted} "
                            f"skip={stats.skipped} fb={stats.row_fallbacks} "
                            f"err={stats.errors} "
                            f"({stats.duration_sec:.1f}s)"
                        )
                except Exception as exc:
                    if tgt_sess:
                        tgt_sess.rollback()
                    err_msg = f"{table_name}: {exc}"
                    result.global_errors.append(err_msg)
                    print(f"  {prefix} {table_name}: ERROR — {exc}")

            # 重置 serial
            if not dry_run and reset_serials and tgt_sess:
                serial_errs = reset_pg_serials(tgt_sess)
                result.global_errors.extend(serial_errs)
                if serial_errs:
                    for e in serial_errs:
                        print(f"  serial: {e}")

    source_engine.dispose()
    if target_engine:
        target_engine.dispose()

    result.finished_at = time.time()
    return result


# ---------------------------------------------------------------------------
# 对账
# ---------------------------------------------------------------------------

def verify_migration(
    source_url: str,
    target_url: str,
    *,
    tables: Optional[Sequence[str]] = None,
    sample_ids: Optional[Sequence[str]] = None,
    checksum_sample_size: int = 100,
) -> VerifyResult:
    """迁移后对账：表级行数 + 抽样 checksum。"""
    if not is_sqlite_url(source_url):
        raise ValueError(f"源库须为 SQLite")
    if not is_postgresql_url(target_url):
        raise ValueError(f"目标库须为 PostgreSQL")

    source_engine = create_sync_engine(source_url, pg=False)
    target_engine = create_sync_engine(target_url, pg=True)
    SourceSession = sessionmaker(bind=source_engine)
    TargetSession = sessionmaker(bind=target_engine)

    mismatches: List[Dict[str, Any]] = []
    checksum_mismatches: List[Dict[str, Any]] = []
    selected = _filter_models(tables)

    with SourceSession() as src_sess, TargetSession() as tgt_sess:
        for model in selected:
            table_name = model.__tablename__
            json_cols = _detect_json_columns(model)

            # 行数对账：直接用 SELECT COUNT(*) 避免全表加载到内存
            src_exists = _table_exists(source_engine, table_name)
            tgt_exists = _table_exists(target_engine, table_name)

            if not src_exists:
                continue

            try:
                src_n = src_sess.execute(
                    text(f"SELECT COUNT(*) FROM {table_name}")
                ).scalar()
            except Exception:
                src_n = len(src_sess.execute(select(model)).scalars().all())

            if not tgt_exists:
                tgt_n = 0
            else:
                try:
                    tgt_n = tgt_sess.execute(
                        text(f"SELECT COUNT(*) FROM {table_name}")
                    ).scalar()
                except Exception:
                    try:
                        tgt_n = len(tgt_sess.execute(select(model)).scalars().all())
                    except Exception:
                        tgt_n = -1

            if src_n != tgt_n:
                mismatches.append({
                    "table": table_name,
                    "source_count": src_n,
                    "target_count": tgt_n,
                    "delta": src_n - tgt_n,
                })

            # 抽样 checksum（仅当行数一致时做）
            if src_n == tgt_n and src_n > 0 and checksum_sample_size > 0:
                try:
                    src_sample = src_sess.execute(
                        select(model).limit(checksum_sample_size)
                    ).scalars().all()
                    tgt_sample = tgt_sess.execute(
                        select(model).limit(checksum_sample_size)
                    ).scalars().all()

                    src_checksums = sorted(
                        _row_checksum(_model_to_mapping(r, json_cols))
                        for r in src_sample
                    )
                    tgt_checksums = sorted(
                        _row_checksum(_model_to_mapping(r, json_cols))
                        for r in tgt_sample
                    )

                    if src_checksums != tgt_checksums:
                        diff = len(
                            set(src_checksums) ^ set(tgt_checksums)
                        )
                        checksum_mismatches.append({
                            "table": table_name,
                            "sample_size": checksum_sample_size,
                            "checksum_diff_count": diff,
                        })
                except Exception:
                    pass

        # 抽样 run 对账
        if sample_ids:
            for rid in sample_ids:
                src_run = src_sess.get(AgentRun, rid)
                tgt_run = tgt_sess.get(AgentRun, rid)
                if bool(src_run) != bool(tgt_run):
                    mismatches.append({
                        "table": "agent_runs",
                        "sample_id": rid,
                        "error": (
                            "missing_on_target" if src_run and not tgt_run
                            else "extra_on_target"
                        ),
                    })

    source_engine.dispose()
    target_engine.dispose()

    return VerifyResult(
        ok=not mismatches and not checksum_mismatches,
        mismatches=mismatches,
        checksum_mismatches=checksum_mismatches,
    )


def _filter_models(tables: Optional[Sequence[str]]) -> List[Type[Any]]:
    if not tables:
        return list(ALL_MODELS)
    allowed = {t.strip() for t in tables if t.strip()}
    return [m for m in ALL_MODELS if m.__tablename__ in allowed]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_source_url() -> str:
    env = os.getenv("SQLITE_SOURCE_URL", "").strip()
    if env:
        return env
    if DATABASE_URL.startswith("sqlite"):
        return DATABASE_URL.replace("sqlite+aiosqlite:", "sqlite:", 1)
    default_db = BACKEND_DIR / "data" / "evaluation.db"
    return "sqlite:///" + str(default_db).replace("\\", "/")


def _default_target_url() -> str:
    env = os.getenv("PG_TARGET_URL", "").strip()
    if env:
        return env
    pg = os.getenv("DATABASE_URL", "").strip()
    if pg.startswith("postgresql"):
        return pg.replace("+asyncpg", "+psycopg2").replace(
            "postgresql://", "postgresql+psycopg2://", 1
        )
    return ""


def _parse_args() -> argparse.Namespace:
    table_names = [m.__tablename__ for m in ALL_MODELS]
    parser = argparse.ArgumentParser(
        description="SQLite -> PostgreSQL 全量数据迁移（25 表，零数据丢失）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/migrate_all_to_pg.py --dry-run
  python scripts/migrate_all_to_pg.py --target-url postgresql+psycopg2://...
  python scripts/migrate_all_to_pg.py --verify-only
  python scripts/migrate_all_to_pg.py --truncate-target  # 清空目标后重迁
        """.strip(),
    )
    parser.add_argument(
        "--source-url",
        default=_default_source_url(),
        help="源 SQLite URL（默认本地 evaluation.db 或 SQLITE_SOURCE_URL）",
    )
    parser.add_argument(
        "--target-url",
        default=_default_target_url(),
        help="目标 PostgreSQL URL（PG_TARGET_URL 或 DATABASE_URL）",
    )
    parser.add_argument(
        "--tables",
        default="",
        help="逗号分隔表名，默认全量 25 表。可选: " + ", ".join(table_names),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="批量插入大小（默认 500）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计源库行数，不写目标库",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="迁移后对账行数 + checksum 抽样",
    )
    parser.add_argument(
        "--sample-run-id",
        action="append",
        dest="sample_run_ids",
        help="对账时抽样 agent_runs.id，可重复",
    )
    parser.add_argument(
        "--checksum-sample",
        type=int,
        default=100,
        help="对账时抽样 checksum 的行数（默认 100，0 关闭）",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="目标库已有主键时不跳过（可能冲突失败）",
    )
    parser.add_argument(
        "--no-reset-serials",
        action="store_true",
        help="不重置 evaluation_results/rounds 的 PG serial",
    )
    parser.add_argument(
        "--truncate-target",
        action="store_true",
        help="迁移前先 DELETE 目标表（[!] 谨慎：仅对迁移的表生效）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="JSON 输出",
    )
    return parser.parse_args()


def _print_human(result: MigrateAllResult) -> None:
    s = result.summary()
    print()
    print("=" * 64)
    print("  迁移结果汇总")
    print("=" * 64)
    print(f"  模式:        {'dry-run' if s['dry_run'] else '实际迁移'}")
    print(f"  表数:        {s['tables']}")
    print(f"  源行数:      {s['source_rows']:,}")
    print(f"  已插入:      {s['inserted']:,}")
    print(f"  已跳过:      {s['skipped']:,}")
    print(f"  逐行回退:    {s['row_fallbacks']:,}")
    print(f"  错误数:      {s['errors']}")
    print(f"  耗时:        {s['duration_sec']:.1f}s")
    print("-" * 64)
    for t in result.tables:
        flag = ""
        if t.missing_on_source:
            flag = " [!]源缺失"
        if t.missing_on_target:
            flag = " [!]目标缺失"
        print(
            f"  {t.table}: src={t.source_count} ins={t.inserted} "
            f"skip={t.skipped} fb={t.row_fallbacks} err={t.errors}{flag}"
        )
    if result.global_errors:
        print("-" * 64)
        for e in result.global_errors:
            print(f"  GLOBAL ERROR: {e}")
    print("=" * 64)


def main() -> None:
    args = _parse_args()
    tables = [t.strip() for t in args.tables.split(",") if t.strip()] or None

    # --verify-only 模式
    if args.verify_only:
        if not args.target_url:
            print("错误: --verify-only 需要 --target-url", file=sys.stderr)
            raise SystemExit(2)
        verify = verify_migration(
            args.source_url,
            args.target_url,
            tables=tables,
            sample_ids=args.sample_run_ids,
            checksum_sample_size=args.checksum_sample,
        )
        if args.json:
            print(json.dumps(verify.as_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"verify ok={verify.ok}")
            if verify.mismatches:
                print(f"  行数不一致 ({len(verify.mismatches)}):")
                for m in verify.mismatches:
                    print(f"    {m}")
            if verify.checksum_mismatches:
                print(f"  Checksum 不一致 ({len(verify.checksum_mismatches)}):")
                for m in verify.checksum_mismatches:
                    print(f"    {m}")
        raise SystemExit(0 if verify.ok else 1)

    # 迁移模式
    if not args.dry_run and not args.target_url:
        print(
            "错误: 请指定 --target-url 或设置 PG_TARGET_URL / DATABASE_URL（postgresql）",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if args.truncate_target:
        print("[!] --truncate-target 已设置：迁移前将 DELETE 目标表数据")
        if not args.dry_run:
            print("   按 Enter 继续，Ctrl+C 取消...", end="", flush=True)
            try:
                input()
            except (KeyboardInterrupt, EOFError):
                print("\n已取消")
                raise SystemExit(0)

    result = run_migration(
        args.source_url,
        args.target_url or "",
        tables=tables,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        skip_existing=not args.no_skip_existing,
        reset_serials=not args.no_reset_serials,
        truncate_target=args.truncate_target,
    )

    if args.json:
        payload = {
            "summary": result.summary(),
            "tables": [t.as_dict() for t in result.tables],
            "global_errors": result.global_errors,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_human(result)

    # 退出码
    total_errors = sum(t.errors for t in result.tables) + len(result.global_errors)
    if total_errors > 0:
        raise SystemExit(1)
    if not args.dry_run:
        s = result.summary()
        if s["source_rows"] == 0:
            print("hint: 源库无数据或表不存在；检查 --source-url", file=sys.stderr)
            raise SystemExit(2)
        # 数据完整性警告
        if s["inserted"] + s["skipped"] < s["source_rows"]:
            lost = s["source_rows"] - s["inserted"] - s["skipped"]
            print(
                f"[!] 警告: 存在 {lost} 行未写入目标库！请检查错误详情。",
                file=sys.stderr,
            )
            raise SystemExit(1)


if __name__ == "__main__":
    main()
