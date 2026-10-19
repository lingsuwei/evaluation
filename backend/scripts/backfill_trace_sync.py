#!/usr/bin/env python3
"""
历史任务 Trace Sync / Builder 回填 CLI（P2 H-03）。

示例：
  cd backend
  python scripts/backfill_trace_sync.py --task-id task_abc
  python scripts/backfill_trace_sync.py --task-id task_abc --task-id task_def --enqueue-only
  python scripts/backfill_trace_sync.py --task-id task_abc --force --include-synced
  python scripts/backfill_trace_sync.py --task-id task_abc --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from database import async_session  # noqa: E402
from services.backfill_trace_sync_service import backfill_trace_sync_for_tasks  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按 task_id 批量入队/重跑 Trace Sync Builder（历史任务回填）",
    )
    parser.add_argument(
        "--task-id",
        action="append",
        dest="task_ids",
        required=True,
        help="评测任务 ID，可重复指定多个",
    )
    parser.add_argument(
        "--status",
        default="pending_sync,failed,pending",
        help="筛选 agent_runs.trace_sync_status，逗号分隔（默认 pending_sync,failed,pending）",
    )
    parser.add_argument(
        "--include-synced",
        action="store_true",
        help="同时包含已 synced 的 Run（等价于扩展 --status）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重跑：重置 pending 任务或重新入队已 synced/failed 的 Run",
    )
    parser.add_argument(
        "--enqueue-only",
        action="store_true",
        help="仅入队，不立即执行 Builder",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="每轮处理的 pending job 数（默认 20）",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=50,
        help="最多处理轮数（默认 50）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计将要处理的 Run/Job，不写库",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出结果",
    )
    return parser.parse_args()


def _print_human(result) -> None:
    summary = result.summary()
    mode = "dry-run" if result.dry_run else "execute"
    print(f"backfill_trace_sync ({mode})")
    print(
        "summary: "
        f"tasks={summary['tasks']} matched={summary['matched']} "
        f"enqueued={summary['enqueued']} reset={summary['reset']} "
        f"skipped_no_trace={summary['skipped_no_trace']} "
        f"skipped_synced={summary['skipped_synced']} "
        f"skipped_active={summary['skipped_active']}"
    )
    if not summary.get("processed") and summary["enqueued"] == 0 and summary["reset"] == 0:
        print("hint: 无 Run 被入队；检查 task_id / --status / Langfuse trace_id")
    for item in result.tasks:
        eq = item.enqueue.as_dict()
        line = (
            f"  task={eq['task_id']} matched={eq['matched']} "
            f"enqueued={eq['enqueued']} reset={eq['reset']}"
        )
        if item.process:
            proc = item.process.as_dict()
            line += (
                f" processed={proc['processed']} success={proc['success']} "
                f"failed={proc['failed']}"
            )
        print(line)


async def _run(args: argparse.Namespace) -> int:
    statuses = tuple(s.strip() for s in args.status.split(",") if s.strip())
    async with async_session() as session:
        result = await backfill_trace_sync_for_tasks(
            session,
            args.task_ids,
            statuses=statuses,
            include_synced=args.include_synced,
            force=args.force,
            enqueue_only=args.enqueue_only,
            batch_size=args.batch_size,
            max_rounds=args.max_rounds,
            dry_run=args.dry_run,
        )

    if args.json:
        payload = {
            "dry_run": result.dry_run,
            "summary": result.summary(),
            "tasks": [
                {
                    "enqueue": t.enqueue.as_dict(),
                    "process": t.process.as_dict() if t.process else None,
                }
                for t in result.tasks
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_human(result)

    summary = result.summary()
    if summary["matched"] == 0:
        return 2
    if not args.dry_run and not args.enqueue_only and summary["failed"] > 0:
        return 1
    return 0


def main() -> None:
    args = _parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
