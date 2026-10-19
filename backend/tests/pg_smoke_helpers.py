"""PostgreSQL smoke 测试共享 helper（Alembic / A-06 迁移）。"""
from __future__ import annotations

import os
from contextlib import contextmanager
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

BACKEND_DIR = Path(__file__).resolve().parents[1]

# P1 agent_runs 基线（无 P2 snapshot 列），供 Alembic 001 add_column / FK 使用
CREATE_AGENT_RUNS_P1_SQL = """
CREATE TABLE agent_runs (
    id VARCHAR PRIMARY KEY,
    origin VARCHAR NOT NULL DEFAULT 'evaluation',
    eval_context JSONB,
    task_id VARCHAR,
    dataset_id VARCHAR,
    question_id INTEGER,
    run_index INTEGER DEFAULT 0,
    agent_id VARCHAR NOT NULL,
    logical_agent_id VARCHAR,
    agent_version VARCHAR,
    agent_name VARCHAR,
    trace_id VARCHAR,
    root_trace_id VARCHAR,
    langfuse_project VARCHAR,
    status VARCHAR NOT NULL,
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,
    tokens_total INTEGER,
    agent_snapshot TEXT NOT NULL,
    error_message TEXT,
    created_at TIMESTAMP,
    trace_sync_status VARCHAR
);
"""


def pg_smoke_enabled() -> bool:
    return os.getenv("PG_ALEMBIC_SMOKE", "").strip().lower() in ("1", "true", "yes")


def pg_smoke_require() -> bool:
    """run_pg_smoke 脚本设置；PG 不可连时 fail 而非 skip。"""
    return os.getenv("PG_SMOKE_REQUIRE", "").strip().lower() in ("1", "true", "yes")


def _pg_unavailable(exc: Exception) -> None:
    msg = f"PostgreSQL 不可用（请先 docker compose up -d postgres）：{exc}"
    if pg_smoke_require():
        pytest.fail(msg)
    pytest.skip(msg)


def pg_admin_url() -> str:
    user = os.getenv("POSTGRES_USER", "onto_eval")
    password = os.getenv("POSTGRES_PASSWORD", "onto_eval_dev")
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/postgres"


def pg_db_url(db_name: str) -> str:
    user = os.getenv("POSTGRES_USER", "onto_eval")
    password = os.getenv("POSTGRES_PASSWORD", "onto_eval_dev")
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db_name}"


def require_psycopg2() -> None:
    try:
        import psycopg2  # noqa: F401
    except ImportError as exc:
        pytest.skip(f"缺少 psycopg2：pip install psycopg2-binary（{exc}）")


def create_pg_admin_engine() -> Engine:
    require_psycopg2()
    return create_engine(pg_admin_url(), isolation_level="AUTOCOMMIT")


def wait_for_postgres(timeout_sec: float = 60.0, interval_sec: float = 2.0) -> bool:
    """轮询 admin 连接，直到 PG 可连或超时。"""
    require_psycopg2()
    deadline = time.monotonic() + timeout_sec
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            admin = create_pg_admin_engine()
            with admin.connect() as conn:
                conn.execute(text("SELECT 1"))
            admin.dispose()
            return True
        except Exception as exc:
            last_exc = exc
            time.sleep(interval_sec)
    if last_exc is not None:
        raise last_exc
    return False


def run_alembic(url: str, *args: str) -> None:
    """子进程跑 Alembic；跳过 app AsyncEngine，迁移 URL 用 psycopg2。"""
    env = os.environ.copy()
    env["ALEMBIC_SKIP_APP_ENGINE"] = "1"
    env["DATABASE_URL"] = url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND_DIR),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        ) from None


def bootstrap_p1_agent_runs(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(CREATE_AGENT_RUNS_P1_SQL))


def ensure_v_run_analytics_view(engine: Engine) -> None:
    from services.run_analytics_view import V_RUN_ANALYTICS_SQL

    with engine.begin() as conn:
        conn.execute(text(V_RUN_ANALYTICS_SQL))


def pg_object_exists(conn, *, kind: str, name: str) -> bool:
    if kind == "table":
        sql = (
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :n"
        )
    elif kind == "column":
        table, column = name.split(".", 1)
        sql = (
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        )
        row = conn.execute(text(sql), {"t": table, "c": column}).fetchone()
        return row is not None
    elif kind == "index":
        sql = (
            "SELECT 1 FROM pg_indexes "
            "WHERE schemaname = 'public' AND indexname = :n"
        )
    elif kind == "view":
        sql = (
            "SELECT 1 FROM information_schema.views "
            "WHERE table_schema = 'public' AND table_name = :n"
        )
    else:
        raise ValueError(f"unknown kind: {kind}")
    row = conn.execute(text(sql), {"n": name}).fetchone()
    return row is not None


@contextmanager
def temporary_pg_database(name_prefix: str) -> Iterator[str]:
    """
    创建临时 PG 库并在退出时 DROP。

    yields: psycopg2 连接 URL（postgresql+psycopg2://.../db_name）
    """
    if not pg_smoke_enabled():
        pytest.skip("设置 PG_ALEMBIC_SMOKE=1 以运行 PostgreSQL smoke test")

    require_psycopg2()
    db_name = f"{name_prefix}_{os.getpid()}"
    admin = create_pg_admin_engine()
    created = False
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        created = True
        yield pg_db_url(db_name)
    except Exception as exc:
        _pg_unavailable(exc)
    finally:
        if created:
            try:
                with admin.connect() as conn:
                    conn.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :db AND pid <> pg_backend_pid()"
                        ),
                        {"db": db_name},
                    )
                    conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
            except Exception:
                pass
        admin.dispose()
