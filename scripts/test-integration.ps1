# Integration tests on a dedicated loopback database. AURA_TEST_DATABASE_NAME selects it
# (default aura_test; must match aura_test(_[a-z0-9]+)?), so worktrees sharing one cluster can
# run in parallel with different names. -Fresh drops and recreates that database first.
param([switch]$Fresh)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$name = $env:AURA_TEST_DATABASE_NAME
if (-not $name) { $name = 'aura_test' }
if ($name -cnotmatch '^aura_test(_[a-z0-9]+)?\z') { throw 'AURA_TEST_DATABASE_NAME must match aura_test(_[a-z0-9]+)?' }
$original = $env:AURA_DATABASE_URL
try {
    $line = Get-Content -LiteralPath '.env' | Where-Object { $_.StartsWith('AURA_DATABASE_URL=') } | Select-Object -First 1
    if (-not $line) { throw 'AURA_DATABASE_URL is missing from .env. Run scripts/bootstrap.ps1 first.' }
    $baseUrl = $line.Substring('AURA_DATABASE_URL='.Length)
    $testUrl = $baseUrl -creplace '/aura$', "/$name"
    if ($testUrl -ceq $baseUrl) { throw 'AURA_DATABASE_URL in .env must name the /aura database.' }
    $env:AURA_DATABASE_URL = $testUrl
    $env:AURA_TEST_DATABASE_URL = $testUrl
    Write-Host "Integration database: $name"
    if ($Fresh) { uv run python scripts/prepare_testdb.py --fresh } else { uv run python scripts/prepare_testdb.py }
    if ($LASTEXITCODE) { throw 'Test database setup failed' }
    uv run alembic upgrade head; if ($LASTEXITCODE) { throw 'Test migration failed' }
    uv run pytest -m integration -p no:cacheprovider; if ($LASTEXITCODE) { throw 'Integration tests failed' }
} finally { $env:AURA_DATABASE_URL = $original; Remove-Item Env:AURA_TEST_DATABASE_URL -ErrorAction SilentlyContinue }
