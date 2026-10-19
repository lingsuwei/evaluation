# H-06 TS-MAN 自动化回归（Windows）
$ErrorActionPreference = "Stop"
$EvalBackend = Join-Path $PSScriptRoot ".."
$OntoEvalRoot = Split-Path (Resolve-Path $EvalBackend) -Parent
$RepoRoot = Split-Path $OntoEvalRoot -Parent
$AgentBackend = Join-Path $RepoRoot "onto-agent\backend"

Set-Location $EvalBackend
Write-Host "==> eval: G-02 / H-03 / H-02 / ingest"
python scripts/verify_push_e2e.py local
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m pytest tests/test_g02_push_e2e.py tests/test_agent_probe_service.py tests/test_runtime_fact_builder.py tests/test_eval_metric_sync_service.py tests/test_fact_ingest.py tests/test_analytics_read_path.py -q --tb=short
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (Test-Path $AgentBackend) {
    Set-Location $AgentBackend
    Write-Host "==> agent: H-01 / H-04"
    python -m pytest tests/test_onto_eval_push.py tests/test_onto_eval_trace_payload.py tests/test_workspace_http_routes.py tests/test_routes_stream.py -q --tb=short
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "==> H-06 自动化回归通过（UI 项 TS-MAN-03 仍需浏览器手工）"
