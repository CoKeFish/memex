"""asuntos: prioridad alta | normal | baja por asunto (texto + corroboración + palabra del usuario)

Revision ID: 0095_asuntos_priority
Revises: 0094_calendar_status_vocabulary
Create Date: 2026-09-10

Evaluación manual del catálogo (2026-09-09, 142 asuntos): cerrar funcionaba (58/59), abrir no
(10/58 bien abiertos). Causas: la semilla «invitación» abría un asunto con UN correo de difusión
masiva; los pagos fallidos se volvían «Pagos <servicio>» y absorbían recibos de rutina; y nada
separaba lo que exige seguimiento de lo pasivo. Decisiones del dueño: invitaciones no son asunto;
los pagos los registra finanzas (asunto solo si hay mora con consecuencia); y una prioridad con
vocabulario estándar (Outlook/Jira): `alta` (plazo, plata o derecho en juego, requisito de algo
mayor, participación activa con entregas), `normal` (hilo vivo sin urgencia), `baja` (seguimiento
pasivo). La pone el asignador leyendo el texto (`priority_source` = llm), la regla del sistema la
corrobora (`rule`) y la palabra del usuario manda (`user`: nadie la pisa).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0095_asuntos_priority"
down_revision: str | None = "0094_calendar_status_vocabulary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_asuntos
            ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'
                CHECK (priority IN ('alta', 'normal', 'baja')),
            ADD COLUMN priority_reason TEXT NOT NULL DEFAULT '',
            ADD COLUMN priority_source TEXT;
        CREATE INDEX ix_mod_asuntos_user_priority
            ON mod_asuntos (user_id, priority) WHERE merged_into_id IS NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS ix_mod_asuntos_user_priority;
        ALTER TABLE mod_asuntos
            DROP COLUMN IF EXISTS priority_source,
            DROP COLUMN IF EXISTS priority_reason,
            DROP COLUMN IF EXISTS priority;
        """
    )
