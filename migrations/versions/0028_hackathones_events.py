"""hackathones: mod_hackathones_events (extractor puro, ADR-015 §11, slice 3)

Revision ID: 0028
Revises: 0026
Create Date: 2026-06-03

Tabla DEL MÓDULO hackathones (patrón `mod_<slug>_*`; espejo de `mod_finance_expenses`). Cada módulo
es dueño de su forma: NO hay tabla central de hechos. `source_inbox_ids BIGINT[]` es la atribución
por-mensaje (sin FK: Postgres no soporta FK sobre array; integridad best-effort, es auditoría).
Fechas nullable: un anuncio suele traer solo el deadline de inscripción y el `name` es el único
campo de dominio obligatorio (el enriquecimiento futuro completa huecos).

Numeración (migration-numbering-worktrees): la cabeza de `main` al crear el worktree es 0026. El
número 0027 YA está reclamado por otros dos worktrees sin mergear (`identidades` → 0027_identidades,
`infra-relaciones-dominios` → 0027_relation_edges), así que se toma el próximo libre 0028 para no
crear un multi-head extra. Al mergear, re-apuntar `down_revision` a la cabeza vigente (o `alembic
merge`) para mantener cadena lineal, igual que hizo 0026 sobre 0025.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mod_hackathones_events (
            id                    BIGSERIAL PRIMARY KEY,
            user_id               BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_inbox_ids      BIGINT[] NOT NULL,
            name                  TEXT NOT NULL,
            starts_on             DATE,
            ends_on               DATE,
            registration_deadline DATE,
            modality              TEXT NOT NULL DEFAULT 'desconocido',
            location              TEXT NOT NULL DEFAULT '',
            url                   TEXT NOT NULL DEFAULT '',
            organizer             TEXT NOT NULL DEFAULT '',
            technologies          TEXT NOT NULL DEFAULT '',
            prizes                TEXT NOT NULL DEFAULT '',
            requirements          TEXT NOT NULL DEFAULT '',
            description           TEXT NOT NULL DEFAULT '',
            evidence              TEXT NOT NULL DEFAULT '',
            metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX mod_hackathones_events_user_starts
            ON mod_hackathones_events (user_id, starts_on);
        CREATE INDEX mod_hackathones_events_inbox_ids
            ON mod_hackathones_events USING GIN (source_inbox_ids);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mod_hackathones_events;")
