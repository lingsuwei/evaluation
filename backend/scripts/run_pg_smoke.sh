#!/usr/bin/env bash
# PostgreSQL 集成 smoke（Alembic + A-06 迁移 + E2E）
#
# WSL / Linux / macOS 请用本脚本：
#   chmod +x scripts/run_pg_smoke.sh
#   ./scripts/run_pg_smoke.sh
#
# Windows PowerShell 请用：.\scripts\run_pg_smoke.ps1
# （不要在 bash 里执行 .ps1，会报 syntax error / $'\r'）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_DIR="$(dirname "$ROOT")"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
elif command -v python >/dev/null 2>&1; then
  PYTHON="$(command -v python)"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  echo "未找到 python；请先: python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt"
  exit 1
fi

# 从项目根 .env 加载 POSTGRES_*（若未 export）
load_postgres_env() {
  local env_file="$COMPOSE_DIR/.env"
  [[ -f "$env_file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line//$'\r'/}"
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^POSTGRES_ ]] || continue
    if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      local key="${BASH_REMATCH[1]}"
      local val="${BASH_REMATCH[2]}"
      val="${val#\"}"; val="${val%\"}"
      val="${val#\'}"; val="${val%\'}"
      if [[ -z "${!key:-}" ]]; then
        export "$key=$val"
      fi
    fi
  done < "$env_file"
}

pick_postgres_port() {
  local host="${POSTGRES_HOST:-127.0.0.1}"
  local user="${POSTGRES_USER:-onto_eval}"
  local password="${POSTGRES_PASSWORD:-onto_eval_dev}"
  for port in "$@"; do
    if "$PYTHON" -c "
import sys
try:
    import psycopg2
    psycopg2.connect(host='${host}', port=${port}, user='${user}', password='${password}', dbname='postgres', connect_timeout=3).close()
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
      export POSTGRES_PORT="$port"
      return 0
    fi
  done
  return 1
}

load_postgres_env

echo "==> 检查 psycopg2"
"$PYTHON" -c "import psycopg2" 2>/dev/null || "$PYTHON" -m pip install -q psycopg2-binary

if command -v docker >/dev/null 2>&1; then
  echo "==> 启动 PostgreSQL（docker compose）"
  if (cd "$COMPOSE_DIR" && docker compose up -d postgres); then
    echo "    等待 PG 就绪..."
    export PG_ALEMBIC_SMOKE=1
    "$PYTHON" -c "from tests.pg_smoke_helpers import wait_for_postgres; wait_for_postgres(timeout_sec=90); print('PostgreSQL ready')" || \
      echo "WARN: PG 未在 90s 内就绪，继续尝试 pytest" >&2
  else
    echo "WARN: docker compose 失败，将尝试连接已有 PG 实例" >&2
  fi
else
  echo "==> 未检测到 docker，假定本地已有 PostgreSQL"
fi

export PG_ALEMBIC_SMOKE=1
export PG_SMOKE_REQUIRE=1
export POSTGRES_USER="${POSTGRES_USER:-onto_eval}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-onto_eval_dev}"
export POSTGRES_HOST="${POSTGRES_HOST:-127.0.0.1}"

if [[ -z "${POSTGRES_PORT:-}" ]]; then
  if pick_postgres_port 5433 5432; then
    echo "==> 自动检测到 PostgreSQL 端口: ${POSTGRES_PORT}"
  else
    export POSTGRES_PORT=5432
    echo "WARN: 未能自动探测 PG 端口，使用默认 5432；可 export POSTGRES_PORT=5433" >&2
  fi
fi

echo "==> 连接: ${POSTGRES_HOST}:${POSTGRES_PORT} (user=${POSTGRES_USER})"
echo "==> 运行 PG 集成 smoke"
"$PYTHON" -m pytest \
  tests/test_alembic_pg_smoke.py \
  tests/test_migrate_pg_smoke.py \
  tests/test_pg_integration_smoke.py \
  -v --tb=short

echo "==> PG smoke 全部通过"
