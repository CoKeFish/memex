"""módulo calendar (ADR-015, slice 2): mod_calendar_events + dedup_candidates

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-30

Storage del segundo módulo de extracción (ADR-015 §6, patrón `mod_<slug>_*`). `calendar` es
el módulo de FECHAS/EVENTOS y, a diferencia de finance, ejercita `provide_domain`: es el
single-writer del dominio consolidado `mod_calendar_events`.

- `mod_calendar_events` — eventos extraídos. Calca `mod_finance_expenses`: `user_id` FK a
  users ON DELETE CASCADE, `source_inbox_ids BIGINT[]` (atribución por-mensaje, sin FK), y
  `evidence`/`metadata`/`created_at`. La fecha/hora se guarda NAIVE (DATE + TIME, sin
  TIMESTAMPTZ): un LLM no resuelve timezone confiablemente desde un correo/chat, así que
  componer un instante absoluto sería falsa precisión. `starts_on` es el ancla (siempre
  presente, indexable); `start_time IS NULL` ⇒ sin hora específica ("todo el día" en el dedup).
- `mod_calendar_dedup_candidates` — marcado del dedup determinista FASE 1 (ADR-015 §4). Una
  fila por PAR de eventos que el pre-filtro barato considera posible duplicado (ventana
  temporal solapada + título/lugar similares). NUNCA borra ni mergea: ambos eventos coexisten;
  acá solo se registra el par + la razón. El par es canónico (`event_a_id < event_b_id`) para
  evitar espejos. `status='candidate'` es el seam de la FASE 2 (desambiguación LLM por par
  ambiguo) que se difiere a un fast-follow: cuando se construya, leerá los `candidate` y los
  pasará a `confirmed`/`rejected`. Se prefiere la tabla de pares a una columna `dedup_group_id`
  en eventos para no forzar agrupación transitiva (A~B, B~C no implica A~C).

FUERA DE ESTE SLICE: la prioridad como dato aportado por otros módulos
(`priority_rank`/`protected`/`override_policy`) NO se modela todavía — no hay módulo
contribuyente (eso lo ejercita hackathones, ADR-015 §11); agregarlo ahora sería especulativo.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mod_calendar_events (
            id               BIGSERIAL PRIMARY KEY,
            user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_inbox_ids BIGINT[] NOT NULL,
            title            TEXT NOT NULL,
            starts_on        DATE NOT NULL,
            ends_on          DATE,
            start_time       TIME,
            end_time         TIME,
            location         TEXT NOT NULL DEFAULT '',
            description      TEXT NOT NULL DEFAULT '',
            evidence         TEXT NOT NULL DEFAULT '',
            metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX mod_calendar_events_user_starts
            ON mod_calendar_events (user_id, starts_on);
        CREATE INDEX mod_calendar_events_inbox_ids
            ON mod_calendar_events USING GIN (source_inbox_ids);
        """
    )

    op.execute(
        """
        CREATE TABLE mod_calendar_dedup_candidates (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            event_a_id  BIGINT NOT NULL REFERENCES mod_calendar_events(id) ON DELETE CASCADE,
            event_b_id  BIGINT NOT NULL REFERENCES mod_calendar_events(id) ON DELETE CASCADE,
            reason      TEXT NOT NULL,
            score       NUMERIC(4,3),
            status      TEXT NOT NULL DEFAULT 'candidate'
                          CHECK (status IN ('candidate','confirmed','rejected')),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (event_a_id < event_b_id),
            UNIQUE (event_a_id, event_b_id)
        );
        CREATE INDEX mod_calendar_dedup_user_status
            ON mod_calendar_dedup_candidates (user_id, status);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mod_calendar_dedup_candidates CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_calendar_events CASCADE;")
