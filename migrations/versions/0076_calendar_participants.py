"""calendar_participants: organizador/asistentes por evento (arista determinista → identidad)

Tabla-hija de `mod_calendar_events` (1 evento raw → N participantes), poblada por el sync de Google
(`sync._replace_participants`, delete+reinsert por evento). Es la materia prima de la arista
determinista evento→identidad (organiza/asiste): el tejedor del grafo (`relations.deterministic`)
resuelve el participante por EMAIL uniendo `email_norm` contra `mod_identidades_identifiers.value_norm`.

`email_norm` se calcula en PYTHON con `norm_identifier('email', …)` al insertar (NO hay normalizador
SQL de email — un join con `lower()` fallaría con Gmail/`+tag`); por eso es una columna materializada,
no generada. Sin UNIQUE: el delete+reinsert por evento garantiza no-duplicados. El índice por
`event_id` sirve el join links→participantes del tejedor.

Numeración (migration-numbering-worktrees): 0076 verificado libre en los 4 worktrees (main +
notificaciones-service @ 0075, identidades-resolution-v3 @ 0074, relevancia-unificada @ 0071); head
lineal = 0075_notifications.

DOWNGRADE: borra la tabla (los participantes se re-capturan en el próximo sync; las aristas
organiza/asiste quedan huérfanas y las barre `relations.maintenance.prune_orphan_edges`).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0076_calendar_participants"  # <= 32 chars (alembic_version.version_num)
down_revision: str | None = "0075_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mod_calendar_event_participants (
            id              BIGSERIAL PRIMARY KEY,
            user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            event_id        BIGINT NOT NULL
                              REFERENCES mod_calendar_events(id) ON DELETE CASCADE,
            role            TEXT NOT NULL CHECK (role IN ('organizer','attendee')),
            display_name    TEXT NOT NULL DEFAULT '',
            email           TEXT NOT NULL DEFAULT '',
            email_norm      TEXT NOT NULL DEFAULT '',
            is_self         BOOLEAN NOT NULL DEFAULT FALSE,
            is_resource     BOOLEAN NOT NULL DEFAULT FALSE,
            response_status TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX mod_calendar_event_participants_event
            ON mod_calendar_event_participants (event_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mod_calendar_event_participants CASCADE;")
