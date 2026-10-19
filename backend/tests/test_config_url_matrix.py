"""数据库 URL 识别与驱动转换矩阵（P0 范式：URL 三分法）。"""
from __future__ import annotations

import pytest

from config import (
    alembic_database_url,
    async_database_url,
    is_postgresql_url,
    is_sqlite_url,
)
from services.migrate_sqlite_to_pg_service import validate_migration_urls

PG_ASYNC = "postgresql+asyncpg://u:p@127.0.0.1:45432/onto_eval_fact"
PG_PSYCOPG2 = "postgresql+psycopg2://u:p@127.0.0.1:45432/onto_eval_fact"
PG_BARE = "postgresql://u:p@127.0.0.1:45432/onto_eval_fact"
SQLITE_AIO = "sqlite+aiosqlite:///./data/evaluation.db"
SQLITE_SYNC = "sqlite:///./data/evaluation.db"
MYSQL = "mysql+pymysql://u:p@127.0.0.1:3306/app"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (SQLITE_AIO, True),
        (SQLITE_SYNC, True),
        (PG_ASYNC, False),
        (PG_PSYCOPG2, False),
        (PG_BARE, False),
        (MYSQL, False),
    ],
)
def test_is_sqlite_url_matrix(url: str, expected: bool):
    assert is_sqlite_url(url) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (PG_ASYNC, True),
        (PG_PSYCOPG2, True),
        (PG_BARE, True),
        (SQLITE_AIO, False),
        (SQLITE_SYNC, False),
        (MYSQL, False),
    ],
)
def test_is_postgresql_url_matrix(url: str, expected: bool):
    assert is_postgresql_url(url) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (PG_ASYNC, PG_PSYCOPG2),
        (PG_PSYCOPG2, PG_PSYCOPG2),
        (PG_BARE, PG_PSYCOPG2),
        (SQLITE_AIO, SQLITE_AIO),
    ],
)
def test_alembic_database_url_matrix(url: str, expected: str):
    assert alembic_database_url(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (PG_PSYCOPG2, PG_ASYNC),
        (PG_ASYNC, PG_ASYNC),
        (PG_BARE, "postgresql+asyncpg://u:p@127.0.0.1:45432/onto_eval_fact"),
        (SQLITE_AIO, SQLITE_AIO),
    ],
)
def test_async_database_url_matrix(url: str, expected: str):
    assert async_database_url(url) == expected


def test_validate_migration_urls_accepts_psycopg2_target():
    """回归：migrate smoke 使用的 psycopg2 目标 URL 须被识别为 PostgreSQL。"""
    validate_migration_urls(
        "sqlite:///tmp/source.db",
        PG_PSYCOPG2,
    )


def test_validate_migration_urls_rejects_non_pg_target():
    with pytest.raises(ValueError, match="目标库须为 PostgreSQL"):
        validate_migration_urls("sqlite:///a.db", SQLITE_SYNC)
