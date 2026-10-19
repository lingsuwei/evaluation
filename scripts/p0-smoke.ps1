# P0 smoke: health, dataset upload, container -> onto-agent connectivity
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Test-Health {
    param([string]$Url, [string]$Label)
    $json = curl.exe -sf $Url
    if ($json -notmatch '"status"\s*:\s*"ok"') {
        throw "$Label failed: $json"
    }
    Write-Host "[ok] $Label"
}

Test-Health "http://127.0.0.1:45000/api/health" "onto-eval /api/health"
Test-Health "http://127.0.0.1:5055/health" "onto-agent /health"

$sample = Join-Path $Root "datasets\onto-eval-simple.json"
if (-not (Test-Path $sample)) { throw "missing sample dataset $sample" }

$uploadJson = curl.exe -sf -X POST "http://127.0.0.1:45000/api/upload" -F "file=@$sample" -F "name=P0-smoke-dataset"
$upload = $uploadJson | ConvertFrom-Json
if (-not $upload.dataset_id) { throw "upload failed: $uploadJson" }
Write-Host "[ok] upload dataset_id=$($upload.dataset_id) item_count=$($upload.item_count)"

$agentCheck = & docker exec onto-eval-app python -c "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:5055/health', timeout=5).read().decode())" 2>&1
if ($agentCheck -notmatch '"status"\s*:\s*"ok"') {
    throw "container cannot reach host.docker.internal:5055 : $agentCheck"
}
Write-Host "[ok] container -> onto-agent"

$dbUrl = (docker exec onto-eval-app printenv DATABASE_URL).Trim()
if ($dbUrl -match "postgresql") {
    Write-Host "[ok] DATABASE_URL (PostgreSQL)=$dbUrl"
} elseif ($dbUrl -match "/app/data/evaluation\.db") {
    Write-Host "[ok] DATABASE_URL (SQLite)=$dbUrl"
} else {
    Write-Warning "[warn] DATABASE_URL 可能配置异常: $dbUrl"
}

if (docker ps --format '{{.Names}}' | Select-String -Pattern '^onto-eval-postgres$' -Quiet) {
    $pgReady = docker exec onto-eval-postgres pg_isready -U onto_eval -d onto_eval_fact 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[ok] postgres pg_isready"
    } else {
        Write-Warning "[warn] onto-eval-postgres 未就绪: $pgReady"
    }
}

Write-Host ""
Write-Host "P0 smoke passed."
