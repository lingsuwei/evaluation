# P2.5 生产门禁验收（G-01 → G-02 → M-05 → E2E）
# 用法: powershell -File scripts/p25-gate.ps1 [-SkipMigrate] [-SkipE2E]
param(
    [switch]$SkipMigrate,
    [switch]$SkipE2E
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Ensure-EnvPgDefaults {
    if (-not (Test-Path ".env")) {
        Copy-Item ".env.example" ".env"
        Write-Host "[p25] 已从 .env.example 创建 .env"
    }
    $lines = Get-Content ".env"
    $map = @{
        "POSTGRES_USER" = "onto_eval"
        "POSTGRES_PASSWORD" = "onto_eval_dev"
        "POSTGRES_DB" = "onto_eval_fact"
        "POSTGRES_PORT" = "5433"
        "TRACE_SYNC_WORKER_ENABLED" = "true"
    }
    foreach ($key in $map.Keys) {
        if ($lines -notmatch "(?m)^$key=") {
            Add-Content -Path ".env" -Value "$key=$($map[$key])"
        }
    }
    $content = Get-Content ".env" -Raw
    if ($content -match "sqlite\+aiosqlite:////app/data/evaluation\.db") {
        $content = $content -replace "DATABASE_URL=sqlite\+aiosqlite:////app/data/evaluation\.db",
            "DATABASE_URL=postgresql+asyncpg://onto_eval:onto_eval_dev@postgres:5432/onto_eval_fact"
        Set-Content -Path ".env" -Value $content -NoNewline
        Write-Host "[p25] 已将 DATABASE_URL 切换为 PostgreSQL（Docker 内 postgres 服务名）"
    }
}

Write-Step "G-01 Phase A: 启动 PostgreSQL"
Ensure-EnvPgDefaults
docker compose up -d postgres
if ($LASTEXITCODE -ne 0) { throw "postgres 启动失败" }

$deadline = (Get-Date).AddSeconds(90)
do {
    $status = docker inspect -f "{{.State.Health.Status}}" onto-eval-postgres 2>$null
    if ($status -eq "healthy") { break }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)
if ($status -ne "healthy") { throw "postgres 未在 90s 内变为 healthy" }
Write-Host "[ok] postgres healthy"

$pgPort = "5433"
if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        if ($_ -match '^\s*POSTGRES_PORT=(.+)$') { $pgPort = $Matches[1].Trim().Trim('"').Trim("'") }
    }
}

Write-Step "G-01 Phase A: PostgreSQL bootstrap（create_all + v_run_analytics + alembic）"
$env:DATABASE_URL = "postgresql+asyncpg://onto_eval:onto_eval_dev@127.0.0.1:${pgPort}/onto_eval_fact"
Push-Location (Join-Path $Root "backend")
try {
    python -m pip install -q psycopg2-binary alembic asyncpg 2>$null
    python -c "from services.pg_bootstrap import bootstrap_postgresql_schema; bootstrap_postgresql_schema()"
    if ($LASTEXITCODE -ne 0) { throw "pg_bootstrap 失败" }
    python -m alembic current
} finally {
    Pop-Location
}
Write-Host "[ok] PostgreSQL bootstrap"

if (-not $SkipMigrate) {
    $sqlitePath = Join-Path $Root "backend\data\evaluation.db"
    if (Test-Path $sqlitePath) {
        Write-Step "G-01 Phase B: SQLite → PG 迁移（可选）"
        Push-Location (Join-Path $Root "backend")
        try {
            python scripts/migrate_runs_to_pg.py --dry-run `
                --source-url "sqlite:///$($sqlitePath -replace '\\','/')" `
                --target-url "postgresql+psycopg2://onto_eval:onto_eval_dev@127.0.0.1:${pgPort}/onto_eval_fact"
            python scripts/migrate_runs_to_pg.py `
                --source-url "sqlite:///$($sqlitePath -replace '\\','/')" `
                --target-url "postgresql+psycopg2://onto_eval:onto_eval_dev@127.0.0.1:${pgPort}/onto_eval_fact"
            python scripts/migrate_runs_to_pg.py --verify-only `
                --source-url "sqlite:///$($sqlitePath -replace '\\','/')" `
                --target-url "postgresql+psycopg2://onto_eval:onto_eval_dev@127.0.0.1:${pgPort}/onto_eval_fact"
        } finally {
            Pop-Location
        }
        Write-Host "[ok] SQLite 迁移完成"
    } else {
        Write-Host "[skip] 无 SQLite 历史库，跳过 Phase B"
    }
}

Write-Step "G-01 Phase C: 重建 app 容器"
docker compose build app
if ($LASTEXITCODE -ne 0) { throw "docker compose build 失败" }
docker compose up -d --force-recreate app
if ($LASTEXITCODE -ne 0) { throw "docker compose up app 失败" }

Start-Sleep -Seconds 8
$dbUrl = (docker exec onto-eval-app printenv DATABASE_URL).Trim()
if ($dbUrl -notmatch "postgresql") {
    throw "DATABASE_URL 未指向 PostgreSQL: $dbUrl"
}
Write-Host "[ok] app DATABASE_URL=$dbUrl"

Write-Step "G-01 Phase D + PG smoke 6 项"
Push-Location (Join-Path $Root "backend")
try {
    & "$Root\backend\scripts\run_pg_smoke.ps1"
    if ($LASTEXITCODE -ne 0) { throw "PG smoke 失败" }
} finally {
    Pop-Location
}

Write-Step "G-02: Builder 逻辑验证"
# verify_push_e2e.py 已在 v1.23 移除（Push/Webhook 端点已删除）
# Builder 逻辑由 test_p2_fact_pipeline 等单测覆盖

Write-Step "M-05: Trace Sync Worker 验证"
Push-Location (Join-Path $Root "backend")
try {
    python -m pytest tests/test_m_features.py -q -k "not ingest_acl"
    if ($LASTEXITCODE -ne 0) { throw "M-05 单测失败" }
} finally {
    Pop-Location
}
Write-Host "[ok] M-05 pytest"

if (-not $SkipE2E) {
    Write-Step "Playwright: 评估历史 → 查看详情 → 三 Tab"
    Push-Location (Join-Path $Root "frontend")
    try {
        if (-not (Test-Path "node_modules/@playwright/test")) {
            npm ci
        }
        npx playwright install chromium 2>$null
        npm run test:e2e -- e2e/tests/eval-detail-tabs.spec.ts
        if ($LASTEXITCODE -ne 0) { throw "Playwright eval-detail-tabs 失败" }
    } finally {
        Pop-Location
    }
    Write-Host "[ok] Playwright eval-detail-tabs"
}

Write-Step "P2.5 门禁验收完成"
Write-Host "请更新 docs/G-01-PG切换Runbook.md §9 签收清单。"
