#!/usr/bin/env python3
"""
SQLite → PostgreSQL 迁移 CLI（P2 A-06）。

前置：
  1. 目标 PG 已 `alembic upgrade head`（或 init_db create_all）
  2. 源 SQLite 为当前 onto-eval 编排 + 事实数据

示例：
  cd backend
  python scripts/migrate_runs_to_pg.py --dry-run
  python scripts/migrate_runs_to_pg.py \\
    --source-url sqlite:///../backend/data/evaluation.db \\
    --target-url postgresql+psycopg2://onto_eval:onto_eval_dev@127.0.0.1:5432/onto_eval_fact
  python scripts/migrate_runs_to_pg.py --verify-only --sample-run-id run_abc
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import DATABASE_URL  # noqa: E402
from services.migrate_sqlite_to_pg_service import (  # noqa: E402
    MIGRATION_MODELS,
    run_migration,
    verify_migration,
)


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
    table_names = [m.__tablename__ for m in MIGRATION_MODELS]
    parser = argparse.ArgumentParser(
        description="SQLite → PostgreSQL 迁移（编排表 + Runtime Fact 表）",
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
        help=f"逗号分隔表名，默认全量。可选: {', '.join(table_names)}",
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
        help="迁移后对账行数（可与 --sample-run-id 抽样）",
    )
    parser.add_argument(
        "--sample-run-id",
        action="append",
        dest="sample_run_ids",
        help="对账时抽样 agent_runs.id，可重复",
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
        "--json",
        action="store_true",
        help="JSON 输出",
    )
    return parser.parse_args()


def _print_human(result) -> None:
    summary = result.summary()
    print(
        f"migrate_runs_to_pg dry_run={summary['dry_run']} "
        f"tables={summary['tables']} source_rows={summary['source_rows']} "
        f"inserted={summary['inserted']} skipped={summary['skipped']} "
        f"errors={summary['errors']}"
    )
    for t in result.tables:
        print(
            f"  {t.table}: source={t.source_count} inserted={t.inserted} "
            f"skipped={t.skipped}"
            + (" (missing_on_source)" if t.missing_on_source else "")
        )
    for err in result.errors:
        print(f"  ERROR: {err}")


def main() -> None:
    args = _parse_args()
    tables = [t.strip() for t in args.tables.split(",") if t.strip()] or None

    if args.verify_only:
        if not args.target_url:
            print("错误: --verify-only 需要 --target-url", file=sys.stderr)
            raise SystemExit(2)
        verify = verify_migration(
            args.source_url,
            args.target_url,
            tables=tables,
            sample_ids=args.sample_run_ids,
        )
        if args.json:
            print(json.dumps(verify.as_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"verify ok={verify.ok} mismatches={len(verify.mismatches)}")
            for m in verify.mismatches:
                print(f"  {m}")
        raise SystemExit(0 if verify.ok else 1)

    if not args.dry_run and not args.target_url:
        print(
            "错误: 请指定 --target-url 或设置 PG_TARGET_URL / DATABASE_URL（postgresql）",
            file=sys.stderr,
        )
        raise SystemExit(2)

    result = run_migration(
        args.source_url,
        args.target_url or "",
        tables=tables,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        skip_existing=not args.no_skip_existing,
        reset_serials=not args.no_reset_serials,
    )

    if args.json:
        payload = {
            "summary": result.summary(),
            "tables": [t.as_dict() for t in result.tables],
            "errors": result.errors,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_human(result)

    if result.errors:
        raise SystemExit(1)
    if not args.dry_run and result.summary()["source_rows"] == 0:
        print("hint: 源库无数据或表不存在；检查 --source-url", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
