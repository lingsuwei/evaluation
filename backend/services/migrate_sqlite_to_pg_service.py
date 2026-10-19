"""
SQLite → PostgreSQL 数据迁移（P2 A-06）。

按 ADR 单库策略迁移编排表 + Runtime Fact 表；支持 dry-run、抽样对账、幂等跳过。
"""
from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Type

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config import alembic_database_url, is_postgresql_url, is_sqlite_url
from database import (
    Agent,
    AgentRun,
    CheckpointResult,
    Dataset,
    EvaluationResult,
    EvaluationRound,
    EvaluationScript,
    EvaluationTask,
    LLMModel,
    ProcessCheckpointTemplate,
    RunMetric,
    RunSpanNode,
    TraceSyncJob,
)

# 迁移顺序：尊重外键 / 引用关系
MIGRATION_MODELS: List[Type[Any]] = [
    Dataset,
    Agent,
    EvaluationScript,
    LLMModel,
    ProcessCheckpointTemplate,
    EvaluationTask,
    AgentRun,
    TraceSyncJob,
    EvaluationResult,
    EvaluationRound,
    RunSpanNode,
    RunMetric,
    CheckpointResult,
]

# 迁移后需重置 PG serial 的表（列名）
_PG_SERIAL_TABLES: Dict[str, str] = {
    "evaluation_results": "id",
    "evaluation_rounds": "id",
}


@dataclass
class TableMigrateStats:
    table: str
    source_count: int = 0
    inserted: int = 0
    skipped: int = 0
    missing_on_source: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "source_count": self.source_count,
            "inserted": self.inserted,
            "skipped": self.skipped,
            "missing_on_source": self.missing_on_source,
        }


@dataclass
class MigrateBatchResult:
    source_url: str
    target_url: str
    dry_run: bool
    tables: List[TableMigrateStats] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "tables": len(self.tables),
            "source_rows": sum(t.source_count for t in self.tables),
            "inserted": sum(t.inserted for t in self.tables),
            "skipped": sum(t.skipped for t in self.tables),
            "errors": len(self.errors),
        }


@dataclass
class VerifyResult:
    ok: bool
    mismatches: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "mismatches": self.mismatches}


def normalize_sync_sqlite_url(url: str) -> str:
    u = (url or "").strip()
    if u.startswith("sqlite+aiosqlite:"):
        return u.replace("sqlite+aiosqlite:", "sqlite:", 1)
    return u


def normalize_sync_pg_url(url: str) -> str:
    return alembic_database_url(url)


def validate_migration_urls(source_url: str, target_url: str) -> None:
    if not is_sqlite_url(source_url):
        raise ValueError(f"源库须为 SQLite，当前: {source_url}")
    if not is_postgresql_url(target_url):
        raise ValueError(f"目标库须为 PostgreSQL，当前: {target_url}")


def create_sync_engine(url: str, *, pg: bool) -> Engine:
    normalized = normalize_sync_pg_url(url) if pg else normalize_sync_sqlite_url(url)
    connect_args = {}
    if normalized.startswith("sqlite:"):
        connect_args["check_same_thread"] = False
    return create_engine(normalized, future=True, connect_args=connect_args)


def _model_to_mapping(row: Any) -> Dict[str, Any]:
    table = row.__class__.__table__
    out: Dict[str, Any] = {}
    for col in table.columns:
        val = getattr(row, col.key)
        if isinstance(val, (dict, list)):
            out[col.key] = val
        elif col.key in ("eval_context", "input_snapshot", "output_snapshot", "attributes", "value_json", "dims", "evidence", "mock_response_payload"):
            if isinstance(val, str):
                try:
                    out[col.key] = json.loads(val)
                except json.JSONDecodeError:
                    out[col.key] = val
            else:
                out[col.key] = val
        else:
            out[col.key] = val
    return out


def _table_exists(engine: Engine, table_name: str) -> bool:
    return inspect(engine).has_table(table_name)


def count_rows(session: Session, model: Type[Any]) -> int:
    return len(session.execute(select(model)).scalars().all())


def migrate_table(
    source_session: Session,
    target_session: Session,
    model: Type[Any],
    *,
    batch_size: int = 500,
    dry_run: bool = False,
    skip_existing: bool = True,
) -> TableMigrateStats:
    table_name = model.__table__.name  # type: ignore[attr-defined]
    stats = TableMigrateStats(table=table_name)

    if not _table_exists(source_session.bind, table_name):  # type: ignore[arg-type]
        stats.missing_on_source = True
        return stats

    rows = source_session.execute(select(model)).scalars().all()
    stats.source_count = len(rows)
    if dry_run or not rows:
        return stats

    mappings = [_model_to_mapping(row) for row in rows]
    pk_cols = [c.key for c in model.__table__.columns if c.primary_key]  # type: ignore[attr-defined]

    for offset in range(0, len(mappings), batch_size):
        batch = mappings[offset : offset + batch_size]
        if skip_existing and pk_cols:
            inserted = _insert_batch_skip_existing(target_session, model, batch, pk_cols[0])
        else:
            target_session.bulk_insert_mappings(model, batch)  # type: ignore[arg-type]
            inserted = len(batch)
        stats.inserted += inserted
        stats.skipped += len(batch) - inserted
        target_session.commit()

    return stats


def _insert_batch_skip_existing(
    session: Session,
    model: Type[Any],
    batch: List[Dict[str, Any]],
    pk_key: str,
) -> int:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    if not batch:
        return 0
    table = model.__table__  # type: ignore[attr-defined]
    stmt = pg_insert(table).values(batch)
    stmt = stmt.on_conflict_do_nothing(index_elements=[pk_key])
    result = session.execute(stmt)
    return result.rowcount or 0


def reset_pg_serials(target_session: Session) -> None:
    for table, col in _PG_SERIAL_TABLES.items():
        if not _table_exists(target_session.bind, table):  # type: ignore[arg-type]
            continue
        target_session.execute(
            text(
                f"""
                SELECT setval(
                    pg_get_serial_sequence('{table}', '{col}'),
                    COALESCE((SELECT MAX({col}) FROM {table}), 1),
                    (SELECT MAX({col}) IS NOT NULL FROM {table})
                )
                """
            )
        )
    target_session.commit()


def run_migration(
    source_url: str,
    target_url: str,
    *,
    tables: Optional[Sequence[str]] = None,
    batch_size: int = 500,
    dry_run: bool = False,
    skip_existing: bool = True,
    reset_serials: bool = True,
) -> MigrateBatchResult:
    if not is_sqlite_url(source_url):
        raise ValueError(f"源库须为 SQLite，当前: {source_url}")
    if not dry_run:
        validate_migration_urls(source_url, target_url)
    result = MigrateBatchResult(
        source_url=source_url,
        target_url=target_url,
        dry_run=dry_run,
    )

    source_engine = create_sync_engine(source_url, pg=False)
    target_engine = None if dry_run else create_sync_engine(target_url, pg=True)
    SourceSession = sessionmaker(bind=source_engine)
    TargetSession = sessionmaker(bind=target_engine) if target_engine else None

    selected = _filter_models(tables)

    with SourceSession() as src_sess:
        tgt_ctx = TargetSession() if TargetSession else nullcontext()
        with tgt_ctx as tgt_sess:
            for model in selected:
                try:
                    stats = migrate_table(
                        src_sess,
                        tgt_sess if tgt_sess else src_sess,
                        model,
                        batch_size=batch_size,
                        dry_run=dry_run,
                        skip_existing=skip_existing,
                    )
                    result.tables.append(stats)
                except Exception as exc:
                    if tgt_sess:
                        tgt_sess.rollback()
                    result.errors.append(f"{model.__tablename__}: {exc}")

            if not dry_run and reset_serials and not result.errors and tgt_sess:
                try:
                    reset_pg_serials(tgt_sess)
                except Exception as exc:
                    result.errors.append(f"reset_serials: {exc}")

    source_engine.dispose()
    if target_engine:
        target_engine.dispose()
    return result


def verify_migration(
    source_url: str,
    target_url: str,
    *,
    tables: Optional[Sequence[str]] = None,
    sample_ids: Optional[Sequence[str]] = None,
) -> VerifyResult:
    """按表行数对账；可选抽样 agent_run id 字段一致性。"""
    validate_migration_urls(source_url, target_url)
    source_engine = create_sync_engine(source_url, pg=False)
    target_engine = create_sync_engine(target_url, pg=True)
    SourceSession = sessionmaker(bind=source_engine)
    TargetSession = sessionmaker(bind=target_engine)

    mismatches: List[Dict[str, Any]] = []
    selected = _filter_models(tables)

    with SourceSession() as src_sess, TargetSession() as tgt_sess:
        for model in selected:
            table_name = model.__table__.name  # type: ignore[attr-defined]
            if not _table_exists(source_engine, table_name):
                continue
            src_n = count_rows(src_sess, model)
            tgt_n = count_rows(tgt_sess, model) if _table_exists(target_engine, table_name) else 0
            if src_n != tgt_n:
                mismatches.append(
                    {
                        "table": table_name,
                        "source_count": src_n,
                        "target_count": tgt_n,
                    }
                )

        if sample_ids:
            for run_id in sample_ids:
                src_run = src_sess.get(AgentRun, run_id)
                tgt_run = tgt_sess.get(AgentRun, run_id)
                if bool(src_run) != bool(tgt_run):
                    mismatches.append(
                        {
                            "table": "agent_runs",
                            "sample_id": run_id,
                            "error": "missing_on_target" if src_run and not tgt_run else "extra_on_target",
                        }
                    )
                elif src_run and tgt_run:
                    for field in ("task_id", "trace_id", "trace_sync_status", "tokens_total"):
                        if getattr(src_run, field) != getattr(tgt_run, field):
                            mismatches.append(
                                {
                                    "table": "agent_runs",
                                    "sample_id": run_id,
                                    "field": field,
                                    "source": getattr(src_run, field),
                                    "target": getattr(tgt_run, field),
                                }
                            )

    source_engine.dispose()
    target_engine.dispose()
    return VerifyResult(ok=not mismatches, mismatches=mismatches)


def _filter_models(tables: Optional[Sequence[str]]) -> List[Type[Any]]:
    if not tables:
        return list(MIGRATION_MODELS)
    allowed = {t.strip() for t in tables if t.strip()}
    return [m for m in MIGRATION_MODELS if m.__tablename__ in allowed]  # type: ignore[attr-defined]
