#!/usr/bin/env python3
"""CLI：legacy aggregate_key 占比报表（P2 I-06）。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from database import async_session  # noqa: E402
from services.legacy_aggregate_report import report_legacy_aggregate_ratio  # noqa: E402


async def _main(task_id: str | None) -> int:
    async with async_session() as session:
        report = await report_legacy_aggregate_ratio(session, task_id=task_id)
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return 1 if report.as_dict().get("alert") else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="legacy aggregate_key 占比")
    parser.add_argument("--task-id", default=None, help="按任务过滤")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.task_id)))


if __name__ == "__main__":
    main()
