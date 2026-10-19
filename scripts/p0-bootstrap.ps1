# P0 部署引导：补齐 .env / langfuse.projects.json，获取 Agent Token，重启 compose
param(
    [switch]$SkipRestart
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Ensure-FileFromExample {
    param([string]$Target, [string]$Example)
    if (-not (Test-Path $Target)) {
        if (-not (Test-Path $Example)) {
            throw "缺少 $Target 且未找到 $Example"
        }
        Copy-Item $Example $Target
        Write-Host "[p0] 已创建 $(Split-Path -Leaf $Target) <- $(Split-Path -Leaf $Example)"
    }
}

function Set-EnvIfMissing {
    param([string]$Key, [string]$Value)
    $envPath = Join-Path $Root ".env"
    $content = Get-Content $envPath -Raw
    if ($content -match "(?m)^$Key=") {
        if ($content -match "(?m)^$Key=\s*$") {
            $content = $content -replace "(?m)^$Key=\s*$", "$Key=$Value"
            Set-Content -Path $envPath -Value $content -NoNewline
            Write-Host "[p0] 已填充 $Key"
        }
    } else {
        Add-Content -Path $envPath -Value "$Key=$Value"
        Write-Host "[p0] 已追加 $Key"
    }
}

Ensure-FileFromExample ".env" ".env.example"
New-Item -ItemType Directory -Path "backend/data" -Force | Out-Null
Ensure-FileFromExample "backend/data/langfuse.projects.json" "langfuse.projects.example.json"

# Docker 容器内数据库路径（P2.5 默认 PostgreSQL）
$envContent = Get-Content ".env" -Raw
if ($envContent -notmatch "(?m)^POSTGRES_USER=") {
    @(
        "POSTGRES_USER=onto_eval",
        "POSTGRES_PASSWORD=onto_eval_dev",
        "POSTGRES_DB=onto_eval_fact",
        "POSTGRES_PORT=5433"
    ) | ForEach-Object { Add-Content -Path ".env" -Value $_ }
    Write-Host "[p0] 已追加 PostgreSQL 默认变量"
    $envContent = Get-Content ".env" -Raw
}
if ($envContent -match "DATABASE_URL=sqlite\+aiosqlite:///\.\\backend") {
    $envContent = $envContent -replace "DATABASE_URL=sqlite\+aiosqlite:///\.\\backend[^\r\n]*", "DATABASE_URL=postgresql+asyncpg://onto_eval:onto_eval_dev@postgres:5432/onto_eval_fact"
    Set-Content -Path ".env" -Value $envContent -NoNewline
    Write-Host "[p0] 已修正 DATABASE_URL 为 Docker PostgreSQL"
} elseif ($envContent -match "DATABASE_URL=sqlite\+aiosqlite:////app/data/evaluation\.db") {
    $envContent = $envContent -replace "DATABASE_URL=sqlite\+aiosqlite:////app/data/evaluation\.db", "DATABASE_URL=postgresql+asyncpg://onto_eval:onto_eval_dev@postgres:5432/onto_eval_fact"
    Set-Content -Path ".env" -Value $envContent -NoNewline
    Write-Host "[p0] 已升级 DATABASE_URL 为 PostgreSQL（P2.5）"
}

if ($envContent -notmatch "ONTO_AGENT_BASE_URL=http://host\.docker\.internal") {
    (Get-Content ".env") -replace "^ONTO_AGENT_BASE_URL=.*", "ONTO_AGENT_BASE_URL=http://host.docker.internal:5055" | Set-Content ".env"
    Write-Host "[p0] 已修正 ONTO_AGENT_BASE_URL"
}

if ((Get-Content ".env" -Raw) -notmatch "TRACE_SYNC_WORKER_ENABLED=") {
    Add-Content -Path ".env" -Value "TRACE_SYNC_WORKER_ENABLED=true"
    Write-Host "[p0] 已追加 TRACE_SYNC_WORKER_ENABLED=true"
}

if (-not $SkipRestart) {
    try {
        $body = '{"username":"admin","password":"admin123"}'
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:5055/auth/login" -Method Post -Body $body -ContentType "application/json"
        if ($resp.token) {
            (Get-Content ".env") -replace "^ONTO_AGENT_API_TOKEN=.*", "ONTO_AGENT_API_TOKEN=$($resp.token)" | Set-Content ".env"
            Write-Host "[p0] 已从 onto-agent 获取 ONTO_AGENT_API_TOKEN"
        }
    } catch {
        Write-Warning "[p0] 无法自动获取 onto-agent Token，请手动设置 ONTO_AGENT_API_TOKEN"
    }
}

if (-not $SkipRestart) {
    docker compose up -d
    if ($LASTEXITCODE -ne 0) { throw "docker compose up 失败" }
    Write-Host "[p0] 容器已重启"
}

Write-Host "[p0] 完成。请运行 scripts/p0-smoke.ps1 验证。"
