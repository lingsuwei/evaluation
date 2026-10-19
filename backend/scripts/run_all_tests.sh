#!/usr/bin/env bash
# 分层测试入口（P2 I / 日常 CI）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -d .venv ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "==> L1 单元/契约（无 integration / postgresql）"
python -m pytest -q -m "not integration and not postgresql"

echo "==> P3 Diff（marker p3，已含于 L1；可单独: pytest -m p3）"

if [[ "${PG_ALEMBIC_SMOKE:-}" == "1" ]]; then
  echo "==> L2 PG smoke"
  python -m pytest tests/test_alembic_pg_smoke.py \
    tests/test_migrate_pg_smoke.py \
    tests/test_pg_integration_smoke.py -v
fi

if [[ "${LANGFUSE_SKIP_LIVE:-}" != "1" ]] && [[ -f data/langfuse.projects.json || -f ../data/langfuse.projects.json || -f ../langfuse.projects.json ]]; then
  echo "==> L3 Langfuse 多项目联调"
  python -m pytest tests/test_langfuse_projects_live.py -v
fi

echo "==> 全部完成"
