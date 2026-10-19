#!/usr/bin/env bash
# P0 smoke: health, dataset upload, container -> onto-agent, DATABASE_URL
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

check_health() {
  local url="$1" label="$2"
  local body
  body="$(curl -sf --connect-timeout 5 "$url")"
  if ! echo "$body" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
    echo "ERROR: $label failed: $body" >&2
    exit 1
  fi
  echo "[ok] $label"
}

check_health "http://127.0.0.1:45000/api/health" "onto-eval /api/health"
check_health "http://127.0.0.1:5055/health" "onto-agent /health"

sample="$ROOT/datasets/onto-eval-simple.json"
if [[ ! -f "$sample" ]]; then
  echo "ERROR: missing sample dataset $sample" >&2
  exit 1
fi

upload_json="$(curl -sf --connect-timeout 10 -X POST "http://127.0.0.1:45000/api/upload" \
  -F "file=@${sample}" -F "name=P0-smoke-dataset")"
dataset_id="$(python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('dataset_id',''))" <<<"$upload_json")"
item_count="$(python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('item_count',''))" <<<"$upload_json")"
if [[ -z "$dataset_id" ]]; then
  echo "ERROR: upload failed: $upload_json" >&2
  exit 1
fi
echo "[ok] upload dataset_id=${dataset_id} item_count=${item_count}"

agent_check="$(docker exec onto-eval-app python -c "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:5055/health', timeout=5).read().decode())" 2>&1 || true)"
if ! echo "$agent_check" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
  echo "ERROR: container cannot reach host.docker.internal:5055 : $agent_check" >&2
  exit 1
fi
echo "[ok] container -> onto-agent"

db_url="$(docker exec onto-eval-app printenv DATABASE_URL | tr -d '\r\n')"
if echo "$db_url" | grep -q 'postgresql'; then
  echo "[ok] DATABASE_URL (PostgreSQL)=${db_url}"
elif echo "$db_url" | grep -q '/app/data/evaluation.db'; then
  echo "[ok] DATABASE_URL (SQLite)=${db_url}"
else
  echo "[warn] DATABASE_URL 可能配置异常: ${db_url}" >&2
fi

# PG 健康（若 compose 含 postgres 服务）
if docker ps --format '{{.Names}}' | grep -qx 'onto-eval-postgres'; then
  if docker exec onto-eval-postgres pg_isready -U "${POSTGRES_USER:-onto_eval}" -d "${POSTGRES_DB:-onto_eval_fact}" >/dev/null 2>&1; then
    echo "[ok] postgres pg_isready"
  else
    echo "[warn] onto-eval-postgres 未就绪" >&2
  fi
fi

echo ""
echo "P0 smoke passed."
