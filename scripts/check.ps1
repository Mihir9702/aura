$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
uv run ruff check packages tests; if ($LASTEXITCODE) { exit $LASTEXITCODE }
uv run mypy packages/aura/src; if ($LASTEXITCODE) { exit $LASTEXITCODE }
uv run pytest -m 'not integration'; if ($LASTEXITCODE) { exit $LASTEXITCODE }
npm run test -w apps/web; if ($LASTEXITCODE) { exit $LASTEXITCODE }
npm run build; exit $LASTEXITCODE
