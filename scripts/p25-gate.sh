#!/usr/bin/env bash
# P2.5 生产门禁验收（G-01 → G-02 → M-05 → E2E）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SKIP_MIGRATE=0
SKIP_E2E=0
for arg in "$@"; do
  case "$arg" in
    --skip-migrate) SKIP_MIGRATE=1 ;;
    --skip-e2e) SKIP_E2E=1 ;;
  esac
done

step() { echo ""; echo "==> $1"; }

step "G-01: 启动 PostgreSQL"
if [[ ! -f .env ]]; then cp .env.example .env; fi
docker compose up -d postgres
for i in $(seq 1 45); do
  status="$(docker inspect -f '{{.State.Health.Status}}' onto-eval-postgres 2>/dev/null || echo starting)"
  [[ "$status" == "healthy" ]] && break
  sleep 2
done
[[ "$status" == "healthy" ]] || { echo "postgres not healthy"; exit 1; }

PG_PORT="${POSTGRES_PORT:-454322}"
if [[ -f .env ]]; then
  val="$(grep -E '^\s*POSTGRES_PORT=' .env | tail -1 | cut -d= -f2- | tr -d '\r"'"'"' ')"
  [[ -n "$val" ]] && PG_PORT="$val"
fi
export DATABASE_URL="postgresql+asyncpg://onto_eval:onto_eval_dev@127.0.0.1:${PG_PORT}/onto_eval_fact"

step "PostgreSQL bootstrap"
cd backend
python -m pip install -q psycopg2-binary alembic asyncpg
python -c "from services.pg_bootstrap import bootstrap_postgresql_schema; bootstrap_postgresql_schema()"
python -m alembic current
cd "$ROOT"

if [[ "$SKIP_MIGRATE" -eq 0 && -f "$ROOT/backend/data/evaluation.db" ]]; then
  step "SQLite → PG 迁移"
  cd "$ROOT/backend"
  python scripts/migrate_runs_to_pg.py \
    --source-url "sqlite:///./data/evaluation.db" \
    --target-url "postgresql+psycopg2://onto_eval:onto_eval_dev@127.0.0.1:${PG_PORT}/onto_eval_fact"
  cd "$ROOT"
fi

step "重建 app"
docker compose build app
docker compose up -d --force-recreate app
sleep 8
docker exec onto-eval-app printenv DATABASE_URL | grep -q postgresql

step "PG smoke"
cd "$ROOT/backend" && ./scripts/run_pg_smoke.sh

step "M-05 pytest"
python -m pytest tests/test_m_features.py -q -k "not ingest_acl"

if [[ "$SKIP_E2E" -eq 0 ]]; then
  step "Playwright eval-detail-tabs"
  cd "$ROOT/frontend"
  npm ci
  npx playwright install chromium
  npm run test:e2e -- e2e/tests/eval-detail-tabs.spec.ts
fi

echo ""
echo "P2.5 gate passed."
