# PostgreSQL 集成 smoke（Alembic + A-06 迁移 + E2E）
# Windows PowerShell 专用。WSL/Linux 请用: ./scripts/run_pg_smoke.sh
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "未找到 python，请先安装 Python 并加入 PATH"
}

Write-Host "==> 检查 psycopg2"
python -c "import psycopg2" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "    安装 psycopg2-binary..."
    python -m pip install -q psycopg2-binary
}

$ComposeDir = Split-Path $Root -Parent
$ComposeFile = Join-Path $ComposeDir "docker-compose.yml"

if (Get-Command docker -ErrorAction SilentlyContinue) {
    Write-Host "==> 启动 PostgreSQL（docker compose）"
    Push-Location $ComposeDir
    try {
        docker compose up -d postgres 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "docker compose 失败，将尝试连接已有 PG 实例"
        } else {
            Write-Host "    等待 PG 就绪..."
            $env:PG_ALEMBIC_SMOKE = "1"
            python -c @"
from tests.pg_smoke_helpers import wait_for_postgres
wait_for_postgres(timeout_sec=90)
print('PostgreSQL ready')
"@
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "PG 未在 90s 内就绪，继续尝试 pytest（可能 skip）"
            }
        }
    } finally {
        Pop-Location
    }
} else {
    Write-Host "==> 未检测到 docker，假定本地已有 PostgreSQL"
}

$env:PG_ALEMBIC_SMOKE = "1"
$env:PG_SMOKE_REQUIRE = "1"
if (-not $env:POSTGRES_USER) { $env:POSTGRES_USER = "onto_eval" }
if (-not $env:POSTGRES_PASSWORD) { $env:POSTGRES_PASSWORD = "onto_eval_dev" }
if (-not $env:POSTGRES_HOST) { $env:POSTGRES_HOST = "127.0.0.1" }
if (-not $env:POSTGRES_PORT) {
    $EnvFile = Join-Path $ComposeDir ".env"
    if (Test-Path $EnvFile) {
        Get-Content $EnvFile | ForEach-Object {
            if ($_ -match '^\s*POSTGRES_PORT=(.+)$') {
                $env:POSTGRES_PORT = $Matches[1].Trim().Trim('"').Trim("'")
            }
        }
    }
    if (-not $env:POSTGRES_PORT) { $env:POSTGRES_PORT = "45432" }
}
Write-Host "==> 运行 PG 集成 smoke"
python -m pytest `
    tests/test_alembic_pg_smoke.py `
    tests/test_migrate_pg_smoke.py `
    tests/test_pg_integration_smoke.py `
    -v --tb=short
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> PG smoke 全部通过"
