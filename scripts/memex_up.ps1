# Levanta TODO memex (API + los 4 daemons + dashboard) con los secretos de Doppler inyectados.
#
# Por qué este script y no `docker compose up -d` a secas:
#  1. Los secretos (DEEPSEEK_API_KEY, el token del bot, ANTHROPIC_API_KEY, OCR/OpenAI) viven en
#     Doppler, no en .env. `doppler run --` los inyecta al crear los contenedores; una vez creados,
#     quedan grabados en la definición del contenedor y sobreviven a los reboots vía
#     `restart: unless-stopped` (Docker Desktop arranca con Windows). O sea: este script se corre a
#     mano CUANDO CAMBIA algo; el reinicio del PC NO lo necesita.
#  2. Sella la versión del código en la imagen del API (igual que build_api.ps1).
#  3. COMPOSE_PROFILES (en .env) arma qué daemons entran; sin él, `up` solo traería API+dashboard.
#
# GOTCHA: NO corras `docker compose up -d` sin Doppler mientras los contenedores existan con
# secretos: compose vería el env vaciarse y RECREARÍA los contenedores sin secretos. Usá siempre
# este script (o `doppler run -- docker compose ...`) para recrear.
#
# Uso: .\scripts\memex_up.ps1 [-Build]   (-Build fuerza reconstruir imágenes)
param([switch]$Build)
$ErrorActionPreference = "Stop"

$sha = (git rev-parse --short=9 HEAD).Trim()
if (git status --porcelain --untracked-files=no) { $sha = "$sha-dirty" }
$env:MEMEX_CODE_VERSION = $sha
Write-Host "MEMEX_CODE_VERSION=$sha"

if ($Build) {
    doppler run -p shared -c dev -- docker compose build
}
doppler run -p shared -c dev -- docker compose up -d
Write-Host ""
docker compose ps --format "{{.Name}}`t{{.Status}}"
