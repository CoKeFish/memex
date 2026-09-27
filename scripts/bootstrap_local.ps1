# scripts/bootstrap_local.ps1
# Bootstrap local dev environment for memex:
#   1. Ensure .env exists (copy from .env.example if not).
#   2. Bring up Postgres via docker compose, wait for healthy.
#   3. Apply Alembic migrations.
#   4. Start the API with --reload.
#
# Usage:  pwsh ./scripts/bootstrap_local.ps1

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "==> Ensuring .env exists" -ForegroundColor Cyan
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "    Created .env from .env.example"
} else {
    Write-Host "    .env already present"
}

Write-Host "==> Starting Postgres (docker compose, --wait for healthy)" -ForegroundColor Cyan
docker compose up -d --wait postgres
if ($LASTEXITCODE -ne 0) { throw "docker compose failed" }

Write-Host "==> Applying Alembic migrations" -ForegroundColor Cyan
uv run alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw "alembic upgrade failed" }

Write-Host "==> Starting API on http://localhost:8787 (Ctrl+C to stop)" -ForegroundColor Cyan
uv run uvicorn memex.api.app:app --reload --port 8787
