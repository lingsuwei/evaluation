#!/usr/bin/env bash
# P5 回归：P5 单测 + P4 + L1 + 可选 PG smoke
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  PYTHON="$(command -v python)"
fi

echo "==> P5 专项（marker p5）"
"$PYTHON" -m pytest -q -m p5

echo "==> P4 专项（marker p4）"
"$PYTHON" -m pytest -q -m p4

echo "==> L1 全量（not integration / not postgresql）"
"$PYTHON" -m pytest -q -m "not integration and not postgresql"

if [[ "${PG_ALEMBIC_SMOKE:-}" == "1" ]]; then
  echo "==> PG smoke"
  bash "$ROOT/scripts/run_pg_smoke.sh"
fi

echo "==> P5 全阶段回归全部通过"
