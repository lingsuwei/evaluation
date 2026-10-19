#!/usr/bin/env bash
# P0 部署引导（Linux 服务器 /data/onto-eval）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --env-only) ENV_ONLY=1 ;;
  esac
done

ensure_file() {
  local target="$1" example="$2"
  if [[ ! -f "$target" ]]; then
    cp "$example" "$target"
    echo "[p0] 已创建 $(basename "$target")"
  fi
}

gen_secret() {
  python3 -c "import secrets; print(secrets.token_urlsafe(32))" 2>/dev/null \
    || openssl rand -base64 32 | tr -d '/+=' | head -c 43
}

set_env_if_empty() {
  local key="$1" val="$2"
  if grep -q "^${key}=$" .env 2>/dev/null || ! grep -q "^${key}=" .env 2>/dev/null; then
    if grep -q "^${key}=" .env 2>/dev/null; then
      sed -i "s|^${key}=.*|${key}=${val}|" .env
    else
      echo "${key}=${val}" >> .env
    fi
    echo "[p0] 已设置 ${key}"
  fi
}

ensure_file ".env" ".env.example"
mkdir -p "$ROOT/backend/data"
ensure_file "$ROOT/backend/data/langfuse.projects.json" "langfuse.projects.example.json"

if grep -q 'DATABASE_URL=sqlite+aiosqlite:///./backend' .env 2>/dev/null; then
  sed -i 's|^DATABASE_URL=sqlite+aiosqlite:///./backend.*|DATABASE_URL=postgresql+asyncpg://onto_eval:onto_eval_dev@postgres:5432/onto_eval_fact|' .env
  echo "[p0] 已修正 DATABASE_URL 为 PostgreSQL（compose 默认）"
fi

if grep -q '^DATABASE_URL=sqlite+aiosqlite:////app/data' .env 2>/dev/null; then
  sed -i 's|^DATABASE_URL=sqlite+aiosqlite:////app/data.*|DATABASE_URL=postgresql+asyncpg://onto_eval:onto_eval_dev@postgres:5432/onto_eval_fact|' .env
  echo "[p0] 已升级 DATABASE_URL SQLite → PostgreSQL"
fi

if ! grep -q 'ONTO_AGENT_BASE_URL=http://host.docker.internal' .env 2>/dev/null; then
  sed -i 's|^ONTO_AGENT_BASE_URL=.*|ONTO_AGENT_BASE_URL=http://host.docker.internal:5055|' .env \
    || echo 'ONTO_AGENT_BASE_URL=http://host.docker.internal:5055' >> .env
  echo "[p0] 已修正 ONTO_AGENT_BASE_URL"
fi

set_env_if_empty "TRACE_SYNC_WORKER_ENABLED" "true"
set_env_if_empty "POSTGRES_USER" "onto_eval"
set_env_if_empty "POSTGRES_PASSWORD" "onto_eval_dev"
set_env_if_empty "POSTGRES_DB" "onto_eval_fact"
set_env_if_empty "POSTGRES_PORT" "5433"

if grep -q 'ONTO_AGENT_API_TOKEN=your_onto_agent_token_here' .env 2>/dev/null; then
  TOKEN=$(curl -sf -X POST http://127.0.0.1:5055/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"username":"admin","password":"admin123"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" || true)
  if [[ -n "$TOKEN" ]]; then
    sed -i "s|^ONTO_AGENT_API_TOKEN=.*|ONTO_AGENT_API_TOKEN=${TOKEN}|" .env
    echo "[p0] 已获取 ONTO_AGENT_API_TOKEN"
  else
    echo "[p0] 警告: 无法自动获取 onto-agent Token"
  fi
fi

if [[ "$ENV_ONLY" -eq 1 ]]; then
  echo "[p0] --env-only：跳过 compose build/up"
  exit 0
fi

docker compose build app
docker compose up -d
echo "[p0] 等待 postgres 健康..."
sleep 3
if docker ps --format '{{.Names}}' | grep -qx 'onto-eval-postgres'; then
  docker exec onto-eval-app sh -c 'cd /app/backend && alembic upgrade head' 2>/dev/null || \
    echo "[p0] 警告: alembic upgrade 失败，请手动在容器内执行"
fi
echo "[p0] 完成。运行 bash scripts/p0-smoke.sh 验证。"
