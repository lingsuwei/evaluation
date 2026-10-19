#!/usr/bin/env bash
# G-06 生产 VM 部署（/data/onto-eval）
# 用法:
#   bash scripts/prod-deploy.sh              # 标准：pull + build + up + alembic + p0-smoke
#   bash scripts/prod-deploy.sh --skip-pull    # 已手动 git pull
#   bash scripts/prod-deploy.sh --full-gate    # 额外跑 p25-gate（跳过 Playwright E2E）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SKIP_PULL=0
SKIP_BUILD=0
FULL_GATE=0
for arg in "$@"; do
  case "$arg" in
    --skip-pull) SKIP_PULL=1 ;;
    --skip-build) SKIP_BUILD=1 ;;
    --full-gate) FULL_GATE=1 ;;
    -h|--help)
      echo "用法: bash scripts/prod-deploy.sh [--skip-pull] [--skip-build] [--full-gate]"
      exit 0
      ;;
  esac
done

step() { echo ""; echo "==> $1"; }

step "0. 前置检查"
command -v docker >/dev/null || { echo "需要 Docker"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "需要 docker compose plugin"; exit 1; }
curl -sf http://127.0.0.1:5055/health >/dev/null 2>&1 || \
  echo "[warn] onto-agent :5055 未响应，评测任务可能失败（p0-smoke 会再检）"

if [[ "$SKIP_PULL" -eq 0 ]]; then
  step "1. git pull"
  git fetch origin
  git pull origin master
fi

step "2. 备份 SQLite（若存在）"
if [[ -f backend/data/evaluation.db ]]; then
  bk="backend/data/evaluation.db.bak.$(date +%Y%m%d_%H%M%S)"
  cp -a backend/data/evaluation.db "$bk"
  echo "[ok] 已备份 -> $bk"
fi

step "3. 环境引导（.env / PG / agent URL）"
bash scripts/p0-bootstrap.sh --env-only

if [[ "$SKIP_BUILD" -eq 0 ]]; then
  step "4. 重建 app 镜像"
  # 镜像拉取失败时在 .env 取消注释 FRONTEND_BASE_IMAGE / PYTHON_BASE_IMAGE
  docker compose build app
  docker compose up -d --force-recreate app
  sleep 10
else
  step "4. 跳过 build，仅 recreate app"
  docker compose up -d --force-recreate app
  sleep 8
fi

step "5. 确认 DATABASE_URL"
db_url="$(docker exec onto-eval-app printenv DATABASE_URL | tr -d '\r\n')"
echo "DATABASE_URL=${db_url}"
echo "$db_url" | grep -q postgresql || { echo "ERROR: 未使用 PostgreSQL"; exit 1; }

step "6. Alembic upgrade（容器内）"
docker exec onto-eval-app sh -c 'cd /app/backend && alembic upgrade head'
docker exec onto-eval-app sh -c 'cd /app/backend && alembic current'

step "7. P0 smoke"
bash scripts/p0-smoke.sh

if [[ "$FULL_GATE" -eq 1 ]]; then
  step "8. P2.5 门禁（跳过 Playwright E2E）"
  bash scripts/p25-gate.sh --skip-e2e || {
    echo "[warn] p25-gate 部分失败，请按 docs/G-06-生产VM部署步骤.md 排查"
    exit 1
  }
fi

echo ""
echo "生产部署完成。下一步："
echo "  - Trace Sync Worker 确认: 检查 TRACE_SYNC_WORKER_ENABLED=true"
echo "  - 功能抽检: docs/G-01-PG切换Runbook.md §6"
echo "  - 签收表: docs/G-01-PG切换Runbook.md §9"
