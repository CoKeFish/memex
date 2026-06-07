"""agente: staging de eventos multi-hecho (start / register* / end)

Revision ID: 0041_agent_event_staging
Revises: 0040_finance_event_id
Create Date: 2026-06-07

El agente (Hermes) abre un evento con `start`, encola N hechos (register de identidad/finanzas/
bienestar) que se CACHEAN acá SIN persistir, y al cerrar con `end` memex los procesa JUNTOS en una
sola transacción, en orden de dependencia (identidad → finanzas → bienestar): la identidad se crea
primero y finanzas ata su contraparte por ID. dedup + consolidación + aristas corren DENTRO de los
`register()` de cada dominio (no se reimplementan).

- `mod_agent_event` = el evento. Un índice parcial único garantiza UN evento ABIERTO por usuario.
  `result` guarda la salida del cierre → reintentar `end` es idempotente (devuelve lo guardado).
- `mod_agent_event_facts` = la cola staged: el `argv` crudo de cada `register`, en orden.

`downgrade` dropea ambas tablas (forward-only; no se preserva el staging al revertir).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0041_agent_event_staging"
down_revision: str | None = "0040_finance_event_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mod_agent_event (
            id         BIGSERIAL PRIMARY KEY,
            user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
            event_id   TEXT,
            result     JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            closed_at  TIMESTAMPTZ
        );
        CREATE UNIQUE INDEX mod_agent_event_one_open
            ON mod_agent_event (user_id) WHERE status = 'open';
        CREATE INDEX mod_agent_event_user_status
            ON mod_agent_event (user_id, status, id DESC);

        CREATE TABLE mod_agent_event_facts (
            id         BIGSERIAL PRIMARY KEY,
            event_fk   BIGINT NOT NULL REFERENCES mod_agent_event(id) ON DELETE CASCADE,
            user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind       TEXT NOT NULL CHECK (kind IN ('identidad','finance','bienestar')),
            argv       JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX mod_agent_event_facts_event ON mod_agent_event_facts (event_fk, id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mod_agent_event_facts CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_agent_event CASCADE;")
