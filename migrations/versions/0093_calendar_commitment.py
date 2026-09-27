"""calendar: nivel de compromiso (firme | probable | informativo) por evento

Revision ID: 0093_calendar_commitment
Revises: 0092_calendar_recurrence
Create Date: 2026-09-09

La importancia de un evento NO se deriva de su procedencia (`protected`/`priority_rank` dicen quién
escribió el dato, no si importa) ni de `kind` (compromiso|anuncio colapsó: 284 vs 3). Experimento
2026-09-09: un juez que LEE el correo acierta 18/18 (el Britano «te confirmamos tus datos de clase»
= firme; «la inscripción se realizará…» = informativo); las señales deterministas solas (pago a la
organización, asunto abierto) dan falsos firmes porque son señales de la ORGANIZACIÓN, no del
evento.

`commitment` lo pone el extractor (lee el texto) o el juez (`memex-calendar-sync classify`) y solo
SUBE por corroboración determinista (probable + pago a la org remitente → firme); `commitment_source`
dice quién lo fijó (extraction | judge | rule | user | manual | provider). `commitment_evidence` es
la frase del mensaje que lo justifica. El consolidado toma el MÁXIMO de sus miembros vivos. Los
choques de horario miran firme y probable; «inamovible» = firme (o `protected`, la palabra del
usuario).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0093_calendar_commitment"
down_revision: str | None = "0092_calendar_recurrence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_calendar_events
            ADD COLUMN commitment TEXT NOT NULL DEFAULT 'informativo'
                CHECK (commitment IN ('firme','probable','informativo')),
            ADD COLUMN commitment_evidence TEXT NOT NULL DEFAULT '',
            ADD COLUMN commitment_source TEXT;
        -- Lo que la persona puso a mano o trae de su propio calendario es firme por definición.
        UPDATE mod_calendar_events SET commitment = 'firme', commitment_source = 'manual'
         WHERE origin = 'manual';
        UPDATE mod_calendar_events
           SET commitment = CASE WHEN provider_status = 'tentative' THEN 'probable' ELSE 'firme' END,
               commitment_source = 'provider'
         WHERE origin = 'provider';
        ALTER TABLE mod_calendar_consolidated
            ADD COLUMN commitment TEXT NOT NULL DEFAULT 'informativo'
                CHECK (commitment IN ('firme','probable','informativo')),
            ADD COLUMN commitment_evidence TEXT NOT NULL DEFAULT '';
        CREATE INDEX mod_calendar_consolidated_commitment
            ON mod_calendar_consolidated (user_id, commitment) WHERE NOT deleted;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS mod_calendar_consolidated_commitment;
        ALTER TABLE mod_calendar_consolidated
            DROP COLUMN IF EXISTS commitment_evidence, DROP COLUMN IF EXISTS commitment;
        ALTER TABLE mod_calendar_events
            DROP COLUMN IF EXISTS commitment_source,
            DROP COLUMN IF EXISTS commitment_evidence,
            DROP COLUMN IF EXISTS commitment;
        """
    )
