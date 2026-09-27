"""Vocabulario estándar: estado del evento confirmado | provisional | anuncio; fuera `kind`;
tarea completada (no «hecha»)

Revision ID: 0094_calendar_status_vocabulary
Revises: 0093_calendar_commitment
Create Date: 2026-09-09

Auditoría de terminología (2026-09-09, pedido del dueño: «firme es un término que nunca he visto
en un calendario»): memex inventaba tres palabras (firme / probable / informativo) donde iCalendar,
Google Calendar y Outlook ya tienen vocabulario: `confirmado` (STATUS:CONFIRMED), `provisional`
(TENTATIVE) y, para lo que no es un compromiso de la persona, `anuncio` — la palabra que ya usaba
`kind`. Con eso `kind` (compromiso | anuncio, 0085) queda redundante: un «anuncio» ES el tercer
estado, así que desaparece de las dos tablas. Tareas: «hecha» → `completada`, como Todoist /
Google Tasks / Microsoft To Do y como ya dice finanzas.

Sin capa de compatibilidad (dato de dev, clean break): se renombran los valores guardados.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0094_calendar_status_vocabulary"
down_revision: str | None = "0093_calendar_commitment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RENAME = """
    CASE commitment WHEN 'firme' THEN 'confirmado'
                    WHEN 'probable' THEN 'provisional'
                    WHEN 'informativo' THEN 'anuncio'
                    ELSE commitment END
"""
_CHECK_NEW = "CHECK (commitment IN ('confirmado','provisional','anuncio'))"
_CHECK_OLD = "CHECK (commitment IN ('firme','probable','informativo'))"


def upgrade() -> None:
    for table in ("mod_calendar_events", "mod_calendar_consolidated"):
        op.execute(
            f"""
            ALTER TABLE {table} DROP CONSTRAINT {table}_commitment_check;
            UPDATE {table} SET commitment = {_RENAME};
            ALTER TABLE {table}
                ALTER COLUMN commitment SET DEFAULT 'anuncio',
                ADD CONSTRAINT {table}_commitment_check {_CHECK_NEW};
            ALTER TABLE {table} DROP COLUMN kind;
            """
        )
    op.execute(
        """
        ALTER TABLE mod_tareas DROP CONSTRAINT mod_tareas_status_check;
        UPDATE mod_tareas SET status = 'completada' WHERE status = 'hecha';
        ALTER TABLE mod_tareas
            ADD CONSTRAINT mod_tareas_status_check
                CHECK (status IN ('pendiente','completada','cancelada'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_tareas DROP CONSTRAINT mod_tareas_status_check;
        UPDATE mod_tareas SET status = 'hecha' WHERE status = 'completada';
        ALTER TABLE mod_tareas
            ADD CONSTRAINT mod_tareas_status_check
                CHECK (status IN ('pendiente','hecha','cancelada'));
        """
    )
    for table in ("mod_calendar_events", "mod_calendar_consolidated"):
        op.execute(
            f"""
            ALTER TABLE {table}
                ADD COLUMN kind TEXT NOT NULL DEFAULT 'compromiso'
                    CHECK (kind IN ('compromiso', 'anuncio'));
            UPDATE {table} SET kind = 'anuncio' WHERE commitment = 'anuncio';
            ALTER TABLE {table} DROP CONSTRAINT {table}_commitment_check;
            UPDATE {table} SET commitment = CASE commitment
                WHEN 'confirmado' THEN 'firme' WHEN 'provisional' THEN 'probable'
                WHEN 'anuncio' THEN 'informativo' ELSE commitment END;
            ALTER TABLE {table}
                ALTER COLUMN commitment SET DEFAULT 'informativo',
                ADD CONSTRAINT {table}_commitment_check {_CHECK_OLD};
            """
        )
