# M-01～M-05 中期能力回归（Windows）
$ErrorActionPreference = "Stop"
$EvalBackend = Join-Path $PSScriptRoot ".."
$OntoEvalRoot = Split-Path (Resolve-Path $EvalBackend) -Parent
$RepoRoot = Split-Path $OntoEvalRoot -Parent
$AgentBackend = Join-Path $RepoRoot "onto-agent\backend"

Set-Location $EvalBackend
Write-Host "==> M-01/M-02/M-05 backend"
python -m pytest tests/test_m_features.py tests/test_diff_summary_service.py tests/test_p3_integration.py -q --tb=short
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (Test-Path $AgentBackend) {
    Set-Location $AgentBackend
    Write-Host "==> M-03 agent attributes"
    python -m pytest tests/test_agent_attributes.py -q --tb=short
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "==> M-01 to M-05 backend regression OK (M-04 Playwright: npm run test:e2e)"
