"""módulos de extracción por intereses (ADR-015): tablas core + finance

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-30

Implementa el storage del primer slice de ADR-015 (extracción modular por intereses):

- `module_settings`  — habilitación por user + perilla MANUAL `batching_policy`
  (per_module/grouped/all) + `config` JSONB. Habilitar un módulo = upsert sobre
  UNIQUE(user_id, module_slug).
- `module_extractions` — cursor de idempotencia: una fila por (module_slug, inbox_id) marca
  que ese mensaje ya pasó por la extracción de ese módulo. El orquestador trackea progreso
  por la AUSENCIA de fila (igual que relations/summary.py con summary_inbox_links). UNIQUE
  (module_slug, inbox_id) + ON CONFLICT DO NOTHING hace la doble-extracción imposible.
- `mod_finance_expenses` — tabla DEL MÓDULO finance (patrón `mod_<slug>_*`). NO hay tabla
  central `extracted_facts`: cada módulo es dueño de su forma. `source_inbox_ids BIGINT[]`
  es la atribución por-mensaje (sin FK: Postgres no soporta FK sobre array; integridad
  best-effort, es auditoría).

FUERA DE ESTE SLICE (aclaración del dueño, 2026-05-30): NO se crea `module_feedback` (es el
substrato de la AUTOMEJORA — loop de feedback de calidad / dial de esfuerzo). Tampoco va el
autofiltro (reglas por módulo). Se construirán con su propia migración cuando se decidan.

El core (`module_settings`/`module_extractions`) NO referencia las tablas de los módulos
(`mod_*`): el acoplamiento va al revés (el módulo conoce el core, no al revés).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE module_settings (
            id              BIGSERIAL PRIMARY KEY,
            user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            module_slug     TEXT NOT NULL,
            enabled         BOOLEAN NOT NULL DEFAULT FALSE,
            batching_policy TEXT NOT NULL DEFAULT 'per_module'
                              CHECK (batching_policy IN ('per_module','grouped','all')),
            config          JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, module_slug)
        );
        CREATE INDEX module_settings_user_enabled ON module_settings (user_id) WHERE enabled;
        """
    )

    op.execute(
        """
        CREATE TABLE module_extractions (
            id           BIGSERIAL PRIMARY KEY,
            user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            module_slug  TEXT NOT NULL,
            inbox_id     BIGINT NOT NULL REFERENCES inbox(id) ON DELETE CASCADE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (module_slug, inbox_id)
        );
        CREATE INDEX module_extractions_inbox ON module_extractions (inbox_id);
        """
    )

    op.execute(
        """
        CREATE TABLE mod_finance_expenses (
            id               BIGSERIAL PRIMARY KEY,
            user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_inbox_ids BIGINT[] NOT NULL,
            amount           NUMERIC(14,2) NOT NULL,
            currency         TEXT NOT NULL,
            merchant         TEXT NOT NULL,
            occurred_on      DATE,
            description      TEXT NOT NULL DEFAULT '',
            evidence         TEXT NOT NULL DEFAULT '',
            metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX mod_finance_expenses_user_created
            ON mod_finance_expenses (user_id, created_at DESC);
        CREATE INDEX mod_finance_expenses_inbox_ids
            ON mod_finance_expenses USING GIN (source_inbox_ids);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mod_finance_expenses CASCADE;")
    op.execute("DROP TABLE IF EXISTS module_extractions CASCADE;")
    op.execute("DROP TABLE IF EXISTS module_settings CASCADE;")
