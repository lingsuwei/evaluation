# G-02 onto-agent Push E2E 验证（Windows）
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$EvalUrl = if ($env:EVAL_URL) { $env:EVAL_URL } else { "http://127.0.0.1:3000" }
$Mode = if ($args.Count -gt 0) { $args[0] } else { "local" }
$Extra = @()
if ($args.Count -gt 1) { $Extra = $args[1..($args.Count - 1)] }

Write-Host "==> G-02 Push E2E mode=$Mode eval=$EvalUrl"
python scripts/verify_push_e2e.py $Mode --eval-url $EvalUrl @Extra
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "==> G-02 verify OK"
