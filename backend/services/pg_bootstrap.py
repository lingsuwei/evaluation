"""PostgreSQL 编排库 + Runtime Fact 层引导（P2.5 G-01）。"""
from __future__ import annotations

import os
import subprocess
import sys

from sqlalchemy import create_engine, text

from config import alembic_database_url
from database import Base
from logger import log
from services.run_analytics_view import V_RUN_ANALYTICS_SQL


def _run_alembic(*args: str, fatal: bool = True) -> bool:
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = os.environ.copy()
    env["DATABASE_URL"] = alembic_database_url(env.get("DATABASE_URL"))
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=backend_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        log(
            "ERROR",
            "DATABASE",
            f"Alembic {' '.join(args)} 失败",
            stderr=result.stderr,
        )
        if fatal:
            raise RuntimeError(result.stderr or f"alembic {' '.join(args)} failed")
        return False
    if result.stdout.strip():
        log("INFO", "DATABASE", f"Alembic {' '.join(args)}", stdout=result.stdout.strip())
    return True


def _alembic_version_exists(engine) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'alembic_version'"
            )
        ).fetchone()
        return row is not None


def _load_valid_alembic_revisions() -> set[str]:
    """扫描 alembic/versions 目录，收集当前代码中定义的全部 revision id。

    用于清理 alembic_version 表中指向已删除/已重命名 revision 的孤儿记录——
    典型场景：migration 文件被重命名（如多 head 合并）后，旧库仍 stamp 着旧 revision，
    导致 upgrade head 报 "Can't locate revision identified by ..."。
    """
    versions_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "alembic", "versions",
    )
    valid: set[str] = set()
    for fname in os.listdir(versions_dir):
        if not fname.endswith(".py") or fname.startswith("__"):
            continue
        path = os.path.join(versions_dir, fname)
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("revision:"):
                        # revision: str = "xxx" / revision = "xxx"
                        if '"' in line:
                            valid.add(line.split('"', 2)[1])
                        break
        except OSError:
            continue
    return valid


def _cleanup_orphan_alembic_revisions(engine) -> None:
    """删除 alembic_version 表中不在当前 migration 链的孤儿 revision。

    幂等：仅在检测到孤儿时删除并记日志。重命名/删除 migration 后旧库的
    stamp 会变成不可达 revision，alembic upgrade head 会失败，需先清理。
    """
    valid = _load_valid_alembic_revisions()
    if not valid:
        return
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
        for (rev,) in rows:
            if rev not in valid:
                conn.execute(
                    text("DELETE FROM alembic_version WHERE version_num = :rev"),
                    {"rev": rev},
                )
                log("INFO", "DATABASE", "清理孤儿 alembic revision", rev=rev)


def _ensure_run_analytics_view(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(V_RUN_ANALYTICS_SQL))
    log("INFO", "DATABASE", "PostgreSQL: v_run_analytics 就绪")


def _ensure_evaluation_task_columns(engine) -> None:
    """Model 新增列尚未写入 Alembic 时的幂等补列（避免 SELECT evaluation_tasks 500）。"""
    columns_sql = (
        ("runtime_metric_targets", "TEXT"),
        ("verdict_thresholds", "TEXT"),
        ("auto_scenario_score", "BOOLEAN DEFAULT true"),
        ("resume_agent_id", "VARCHAR"),
        ("agent_ids", "TEXT"),
    )
    with engine.begin() as conn:
        existing = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'evaluation_tasks'"
                )
            )
        }
        for col, col_type in columns_sql:
            if col in existing:
                continue
            conn.execute(text(f"ALTER TABLE evaluation_tasks ADD COLUMN {col} {col_type}"))
            log("INFO", "DATABASE", f"PostgreSQL: 补列 evaluation_tasks.{col}")


def _ensure_agent_run_columns(engine) -> None:
    """agent_runs 新增列的幂等补列（trace 精确查找用）。"""
    columns_sql = (
        ("onto_agent_run_id", "VARCHAR"),
        ("onto_agent_session_id", "VARCHAR"),
    )
    with engine.begin() as conn:
        existing = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'agent_runs'"
                )
            )
        }
        for col, col_type in columns_sql:
            if col in existing:
                continue
            conn.execute(text(f"ALTER TABLE agent_runs ADD COLUMN {col} {col_type}"))
            log("INFO", "DATABASE", f"PostgreSQL: 补列 agent_runs.{col}")


def _ensure_custom_metric_columns(engine) -> None:
    """custom_metrics 新增列的幂等补列（避免 SELECT custom_metrics 500）。"""
    columns_sql = (
        ("category", "VARCHAR"),
    )
    with engine.begin() as conn:
        existing = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'custom_metrics'"
                )
            )
        }
        for col, col_type in columns_sql:
            if col in existing:
                continue
            conn.execute(text(f"ALTER TABLE custom_metrics ADD COLUMN {col} {col_type}"))
            log("INFO", "DATABASE", f"PostgreSQL: 补列 custom_metrics.{col}")


def _ensure_trace_sync_jobs_columns(engine) -> None:
    """trace_sync_jobs 新增列的幂等补列（失败分类用）。"""
    columns_sql = (
        ("failure_category", "VARCHAR"),
    )
    with engine.begin() as conn:
        existing = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'trace_sync_jobs'"
                )
            )
        }
        for col, col_type in columns_sql:
            if col in existing:
                continue
            conn.execute(text(f"ALTER TABLE trace_sync_jobs ADD COLUMN {col} {col_type}"))
            log("INFO", "DATABASE", f"PostgreSQL: 补列 trace_sync_jobs.{col}")


def bootstrap_postgresql_schema() -> None:
    """
    PostgreSQL 引导策略（避免 create_all 与 Alembic 增量迁移重复建表）：

    - 已有 alembic_version：仅 ``upgrade head``，再确保 v_run_analytics 视图存在。
    - 全新库：``create_all`` + v_run_analytics + ``stamp head``（不写 alembic_version 前不跑 upgrade）。
    """
    sync_url = alembic_database_url()
    engine = create_engine(sync_url)
    try:
        if _alembic_version_exists(engine):
            log("INFO", "DATABASE", "PostgreSQL: 已有 Alembic 记录")
            # 先补列，避免 upgrade 失败时 ORM 查询 evaluation_tasks 500/502
            _ensure_evaluation_task_columns(engine)
            # 清理 alembic_version 表中指向已重命名/删除 revision 的孤儿记录
            # （migration 链调整后旧库 stamp 可能不可达，导致 upgrade head 失败）
            _cleanup_orphan_alembic_revisions(engine)
            upgraded = _run_alembic("upgrade", "head", fatal=False)
            if not upgraded:
                log(
                    "WARNING",
                    "DATABASE",
                    "Alembic upgrade head 未成功，尝试 upgrade heads",
                )
                upgraded = _run_alembic("upgrade", "heads", fatal=False)
            if not upgraded:
                log(
                    "WARNING",
                    "DATABASE",
                    "Alembic upgrade 未成功，已跳过；evaluation_tasks 缺列已尝试补全，"
                    "请稍后手动执行 alembic upgrade head",
                )
            _ensure_run_analytics_view(engine)
            _ensure_evaluation_task_columns(engine)
            _ensure_agent_run_columns(engine)
            _ensure_custom_metric_columns(engine)
            _ensure_trace_sync_jobs_columns(engine)
        else:
            log("INFO", "DATABASE", "PostgreSQL bootstrap: 新库 create_all")
            Base.metadata.create_all(engine)
            _ensure_run_analytics_view(engine)
            _ensure_evaluation_task_columns(engine)
            _ensure_agent_run_columns(engine)
            _ensure_custom_metric_columns(engine)
            _ensure_trace_sync_jobs_columns(engine)
            log("INFO", "DATABASE", "PostgreSQL: alembic stamp head（新库）")
            _run_alembic("stamp", "head")
    finally:
        engine.dispose()
    log("INFO", "DATABASE", "PostgreSQL bootstrap 完成")
