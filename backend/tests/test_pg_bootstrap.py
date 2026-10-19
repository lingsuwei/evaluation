"""PostgreSQL bootstrap：避免 create_all 与 Alembic 重复建表。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services import pg_bootstrap


@pytest.fixture()
def mock_engine():
    engine = MagicMock()
    conn_ctx = MagicMock()
    engine.begin.return_value.__enter__ = MagicMock(return_value=MagicMock())
    engine.begin.return_value.__exit__ = MagicMock(return_value=False)
    conn_ctx.__enter__ = MagicMock(return_value=MagicMock())
    conn_ctx.__exit__ = MagicMock(return_value=False)
    engine.connect.return_value = conn_ctx
    return engine


def test_bootstrap_existing_db_skips_create_all(mock_engine):
    with (
        patch.object(pg_bootstrap, "create_engine", return_value=mock_engine),
        patch.object(pg_bootstrap, "_alembic_version_exists", return_value=True),
        patch.object(pg_bootstrap, "_run_alembic") as run_alembic,
        patch.object(pg_bootstrap.Base.metadata, "create_all") as create_all,
        patch.object(pg_bootstrap, "_ensure_run_analytics_view") as ensure_view,
        patch.object(pg_bootstrap, "_ensure_evaluation_task_columns") as ensure_cols,
    ):
        pg_bootstrap.bootstrap_postgresql_schema()

    create_all.assert_not_called()
    run_alembic.assert_called_once_with("upgrade", "head", fatal=False)
    ensure_view.assert_called_once_with(mock_engine)
    assert ensure_cols.call_count == 2
    mock_engine.dispose.assert_called_once()


def test_bootstrap_existing_db_continues_when_alembic_fails(mock_engine):
    with (
        patch.object(pg_bootstrap, "create_engine", return_value=mock_engine),
        patch.object(pg_bootstrap, "_alembic_version_exists", return_value=True),
        patch.object(pg_bootstrap, "_run_alembic", return_value=False) as run_alembic,
        patch.object(pg_bootstrap.Base.metadata, "create_all") as create_all,
        patch.object(pg_bootstrap, "_ensure_run_analytics_view") as ensure_view,
        patch.object(pg_bootstrap, "_ensure_evaluation_task_columns") as ensure_cols,
    ):
        pg_bootstrap.bootstrap_postgresql_schema()

    create_all.assert_not_called()
    assert run_alembic.call_args_list == [
        (("upgrade", "head"), {"fatal": False}),
        (("upgrade", "heads"), {"fatal": False}),
    ]
    ensure_view.assert_called_once_with(mock_engine)
    assert ensure_cols.call_count == 2


def test_bootstrap_new_db_uses_create_all_and_stamp(mock_engine):
    with (
        patch.object(pg_bootstrap, "create_engine", return_value=mock_engine),
        patch.object(pg_bootstrap, "_alembic_version_exists", return_value=False),
        patch.object(pg_bootstrap, "_run_alembic") as run_alembic,
        patch.object(pg_bootstrap.Base.metadata, "create_all") as create_all,
        patch.object(pg_bootstrap, "_ensure_run_analytics_view") as ensure_view,
        patch.object(pg_bootstrap, "_ensure_evaluation_task_columns") as ensure_cols,
    ):
        pg_bootstrap.bootstrap_postgresql_schema()

    create_all.assert_called_once_with(mock_engine)
    ensure_view.assert_called_once_with(mock_engine)
    ensure_cols.assert_called_once_with(mock_engine)
    run_alembic.assert_called_once_with("stamp", "head")
    mock_engine.dispose.assert_called_once()
