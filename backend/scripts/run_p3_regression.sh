#!/usr/bin/env bash
# P3-A 回归：P3 单测 + L1 全量 + 可选 PG smoke
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

echo "==> P3 专项（marker p3）"
"$PYTHON" -m pytest -q -m p3

echo "==> L1 全量（not integration / postgresql）"
"$PYTHON" -m pytest -q -m "not integration and not postgresql"

if [[ "${PG_ALEMBIC_SMOKE:-}" == "1" ]]; then
  echo "==> PG smoke"
  bash "$ROOT/scripts/run_pg_smoke.sh"
fi

echo "==> P3 全阶段回归全部通过"
