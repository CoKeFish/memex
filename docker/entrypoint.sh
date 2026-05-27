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

echo "[entrypoint] aplicando migraciones..."
.venv/bin/alembic upgrade head

echo "[entrypoint] arrancando: $*"
exec .venv/bin/"$@"
