# Reconstruye y levanta SOLO la imagen del API con el sello de versión del código (memex.version).
# `docker compose build api` a secas deja MEMEX_CODE_VERSION=unknown porque .git no entra al contexto.
#
# Pasa por `doppler run` a propósito: `docker compose up -d api` sin Doppler ve vaciarse el env de
# secretos (token del bot, ANTHROPIC, OCR) y RECREA el contenedor sin ellos — el API se queda sin
# poder mandar por Telegram. Mismo motivo en scripts/memex_up.ps1.
#
# Para levantar TODO (API + daemons + dashboard): .\scripts\memex_up.ps1 [-Build]
# Uso: .\scripts\build_api.ps1 [-NoUp]
param([switch]$NoUp)
$ErrorActionPreference = "Stop"
$sha = (git rev-parse --short=9 HEAD).Trim()
$dirty = (git status --porcelain --untracked-files=no)
if ($dirty) { $sha = "$sha-dirty" }
$env:MEMEX_CODE_VERSION = $sha
Write-Host "MEMEX_CODE_VERSION=$sha"
doppler run -p shared -c dev -- docker compose build api
if (-not $NoUp) { doppler run -p shared -c dev -- docker compose up -d api }
