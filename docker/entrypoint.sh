#!/usr/bin/env bash
# Espera a que Postgres responda, aplica migraciones, y lanza el comando final
# (típicamente uvicorn). Si las migraciones fallan, el container muere — es
# preferible un fail-fast a quedar sirviendo contra schema inconsistente.

set -euo pipefail

echo "[entrypoint] esperando a Postgres..."
.venv/bin/python - <<'PY'
import os, time, sys
import psycopg
url = os.environ["MEMEX_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    try:
        with psycopg.connect(url, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        print("[entrypoint] Postgres OK")
        sys.exit(0)
    except Exception as e:
        print(f"[entrypoint] Postgres aún no: {type(e).__name__}: {e}")
        time.sleep(2)
print("[entrypoint] Postgres NO respondió en 60s")
sys.exit(1)
PY

# Migraciones: las aplica UN solo proceso (el api, cuyo comando es `uvicorn`). Los daemons
# (scheduler / ingest-scheduler, comando `python -m ...`) las OMITEN para no correr `alembic upgrade
# head` los tres a la vez: el upgrade es transaccional, así que la carrera no corrompe nada, pero los
# perdedores logueaban un ERROR "expected to match one row ... 0 found" en cada deploy (cosmético).
# Override explícito con MEMEX_RUN_MIGRATIONS=1/0 (compose-go NO soporta el merge YAML `<<`, por eso
# el gate va por comando y no por env por-servicio).
run_migrations="${MEMEX_RUN_MIGRATIONS:-}"
if [ -z "$run_migrations" ]; then
  [ "${1:-}" = "uvicorn" ] && run_migrations=1 || run_migrations=0
fi
if [ "$run_migrations" = "1" ]; then
  echo "[entrypoint] aplicando migraciones..."
  .venv/bin/alembic upgrade head
else
  echo "[entrypoint] migraciones OMITIDAS (las aplica el api)"
fi

echo "[entrypoint] arrancando: $*"
exec .venv/bin/"$@"
