"""log_events: persistencia consultable de TODOS los eventos structlog

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-02

Hasta acá structlog rendereaba a stderr y nada más: los 126+ nombres de evento del código (HTTP,
auth, ruteo, streaming, OCR, fallos de worker, errores no-LLM) se PERDÍAN al salir el proceso, así
que el dashboard solo podía RECONSTRUIR un "stream" desde `llm_calls`. Esta tabla es el log sink: un
processor de structlog (`memex.core.log_sink`) persiste cada evento (sobre un umbral de nivel) acá,
de forma no bloqueante (cola en memoria + escritor por lotes). El endpoint `/logs` la consulta.

Decisiones de esquema:
- `user_id` SÍ es FK (ON DELETE SET NULL): multi-tenant desde día 1, pero nullable porque hay líneas
  pre-auth / de infraestructura (boot, health-check) que no tienen usuario; se conservan (feed de
  debug completo).
- `source_id` / `inbox_id` NO son FK a propósito: un log es append-only y NUNCA debe fallar el INSERT
  por una referencia borrada o sintética. Son solo correlación (vienen de los contextvars).
- `fields` JSONB = el resto de kwargs estructurados del evento (model, cost_usd, latency_ms, ...).
- `exception` = traceback ya formateado (string) cuando el evento llevaba exc_info.

`llm_calls` sigue siendo la fuente de verdad de COSTO (la vista /metricas agrega ahí). `log_events`
es el feed de LÍNEAS de log; las llamadas LLM caen acá también (event='llm.call', vía record_llm_call)
pero NO se suman entre tablas: mismo hecho, dos ángulos.

Búsqueda: `q` usa ILIKE sobre event + fields::text + exception (suficiente al volumen actual). Un
upgrade futuro a full-text real sería una columna `tsvector` generada + índice GIN; no se construye
ahora.

Retención: el job `log_purge` del scheduler (OFF por default) borra filas más viejas que
MEMEX_LOG_PERSIST_RETENTION_DAYS. Sin él, la tabla crece sin límite — prenderlo tras el rollout.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE log_events (
            id         BIGSERIAL PRIMARY KEY,
            ts         TIMESTAMPTZ NOT NULL,
            level      TEXT NOT NULL,
            event      TEXT NOT NULL,
            logger     TEXT,
            user_id    BIGINT REFERENCES users(id) ON DELETE SET NULL,
            request_id TEXT,
            run_id     TEXT,
            source_id  BIGINT,
            inbox_id   BIGINT,
            exception  TEXT,
            fields     JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        CREATE INDEX log_events_ts        ON log_events (ts DESC);
        CREATE INDEX log_events_level_ts  ON log_events (level, ts DESC);
        CREATE INDEX log_events_event_ts  ON log_events (event, ts DESC);
        CREATE INDEX log_events_user_ts   ON log_events (user_id, ts DESC);
        CREATE INDEX log_events_request   ON log_events (request_id) WHERE request_id IS NOT NULL;
        CREATE INDEX log_events_run       ON log_events (run_id) WHERE run_id IS NOT NULL;
        CREATE INDEX log_events_fields_gin ON log_events USING GIN (fields);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS log_events CASCADE;")
