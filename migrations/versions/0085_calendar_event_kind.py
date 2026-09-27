"""Calendario: `kind` = compromiso | anuncio en eventos crudos y consolidados

Revision ID: 0085_calendar_event_kind
Revises: 0084_rule_audits
Create Date: 2026-09-05

Hasta acá calendario solo registraba eventos DE la persona (citas, clases, exámenes, vuelos…) y
excluía cualquier fecha de un anuncio. Resultado: un correo que el gate deja pasar por un interés
(«hackathon que arranca el 14 de septiembre») no dejaba ninguna fecha registrada. Decisión del
dueño (2026-09-05): las convocatorias y eventos abiertos con fecha SÍ se registran, marcados como
`anuncio`, para tenerlos en el mapa sin confundirlos con compromisos. Criterio general, no atado a
un tipo de evento.

- `compromiso` (default): lo que la persona tiene que hacer o a lo que va (el comportamiento previo).
- `anuncio`: convocatoria/evento abierto/plazo con fecha que el mensaje anuncia.
El consolidado hereda `compromiso` si CUALQUIER miembro lo es (una inscripción posterior convierte
el anuncio en compromiso). El daemon de transporte y los conflictos de agenda ignoran `anuncio`.

Numeración (migration-numbering-worktrees): head lineal = 0084; el único otro worktree vivo
(relevancia-gate-regex) está en 0077 → 0085 libre.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0085_calendar_event_kind"
down_revision: str | None = "0084_rule_audits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND = "TEXT NOT NULL DEFAULT 'compromiso' CHECK (kind IN ('compromiso', 'anuncio'))"


def upgrade() -> None:
    op.execute(f"ALTER TABLE mod_calendar_events ADD COLUMN kind {_KIND}")
    op.execute(f"ALTER TABLE mod_calendar_consolidated ADD COLUMN kind {_KIND}")


def downgrade() -> None:
    op.execute("ALTER TABLE mod_calendar_consolidated DROP COLUMN kind")
    op.execute("ALTER TABLE mod_calendar_events DROP COLUMN kind")
