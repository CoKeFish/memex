"""scheduler_settings: control runtime del daemon (prender/apagar + qué jobs) desde la DB

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-02

Hasta acá el daemon `memex-scheduler` leía `MEMEX_SCHEDULER_ENABLED_JOBS` (CSV) SOLO del env al
arrancar. Esta tabla es la fuente de verdad en RUNTIME: el endpoint PATCH /processing/scheduler la
escribe y el loop del daemon la relee cada tick (`_reload_jobs_if_needed`), así el toggle de la UI
prende/apaga sin reiniciar el proceso.

Filosofía "apagado por default" (config.py): sin fila o `enabled_jobs=''` → desarmado, no procesa nada.
Una fila por usuario (single-tenant hoy, multi-tenant-ready). `daemon_enabled=FALSE` deja el daemon
idle aunque haya jobs listados.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE scheduler_settings (
            user_id        BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            daemon_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            enabled_jobs   TEXT    NOT NULL DEFAULT '',
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS scheduler_settings CASCADE;")
