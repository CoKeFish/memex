"""Gate de relevancia por intereses personales (correos): tablas del módulo + etapa dead-letter

Revision ID: 0065_relevance_gate
Revises: 0064_relation_edge_decisions
Create Date: 2026-06-12

Portero que corre ANTES de todo procesamiento LLM (resumen + ruteo/extracción), SOLO para
correos (SourceKind email). Motivación: el router descarta promos con varianza («NO publicidad»
en los `interest` de los módulos) → correos de intereses reales del dueño quedan invisibles.

- `personal_interests`: los intereses del usuario en texto libre (ej. «descuentos de Steam»).
  Son el contexto que consume el LLM del gate. CRUD por API/CLI; `enabled` permite apagar un
  interés sin perderlo.
- `relevance_gate_rules`: reglas DETERMINISTAS del propio gate. Las propone el LLM (segunda
  pasada de minería sobre los no-relevantes) o el dueño a mano. Toda propuesta pasa por un DRY
  RUN contra el histórico: si matchearía algún correo relevante, la regla está mal hecha y queda
  `rejected` CON su reporte (auditoría); si pasa, se AUTO-ACTIVA (`activated_at`). Reversible:
  active↔disabled. El gate las aplica ANTES de llamar al LLM (determinismo primero, ahorra Opus).
- `relevance_verdicts`: el cursor del gate, una fila por mensaje (UNIQUE inbox_id). La AUSENCIA
  de fila = pendiente-de-gate (con el gate encendido, un correo sin veredicto NO se procesa).
  `method` distingue regla/llm/manual; `mode` registra el experimento per_window vs per_message.
  El override manual canónico sigue siendo `relevance_marks` (0049) y GANA siempre sobre el
  veredicto; resolver un `insufficient` escribe AMBAS (mark + verdict→manual) en una tx.
- `relevance_gate_settings`: settings por usuario (patrón `scheduler_settings`), tabla PROPIA y
  no `module_settings`: el gate no es un InterestModule (un slug ahí rompería `resolve()` del
  registry y `PATCH /modules/{slug}`). `enabled` default FALSE — apagado por default.
- `work_item_failures.stage`: se amplía el CHECK con 'relevance' para que el gate tenga
  dead-letter propio (ventanas veneno que nunca parsean). El nombre del constraint es el
  autogenerado por Postgres para el CHECK inline de la 0012 (`<tabla>_<columna>_check`).

Numeración (migration-numbering-worktrees): 0065 verificado libre en todas las ramas/worktrees;
head lineal = 0064_relation_edge_decisions.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0065_relevance_gate"
down_revision: str | None = "0064_relation_edge_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE personal_interests (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            text        TEXT NOT NULL CHECK (length(btrim(text)) > 0),
            enabled     BOOLEAN NOT NULL DEFAULT TRUE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, text)
        );
        CREATE INDEX personal_interests_user ON personal_interests (user_id);

        CREATE TABLE relevance_gate_rules (
            id             BIGSERIAL PRIMARY KEY,
            user_id        BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind           TEXT NOT NULL CHECK (
                               kind IN ('sender_email','sender_domain','subject_contains','list_id')
                           ),
            pattern        TEXT NOT NULL CHECK (length(btrim(pattern)) > 0),
            status         TEXT NOT NULL CHECK (status IN ('active','disabled','rejected')),
            proposed_by    TEXT NOT NULL CHECK (proposed_by IN ('llm','manual')),
            rationale      TEXT NOT NULL DEFAULT '',
            dry_run_report JSONB NOT NULL DEFAULT '{}'::jsonb,
            model          TEXT,
            activated_at   TIMESTAMPTZ,
            deactivated_at TIMESTAMPTZ,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, kind, pattern)
        );
        CREATE INDEX relevance_gate_rules_user_status
            ON relevance_gate_rules (user_id, status);

        CREATE TABLE relevance_verdicts (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            inbox_id    BIGINT NOT NULL REFERENCES inbox(id) ON DELETE CASCADE,
            verdict     TEXT NOT NULL CHECK (verdict IN ('relevant','not_relevant','insufficient')),
            method      TEXT NOT NULL CHECK (method IN ('rule','llm','manual')),
            rule_id     BIGINT REFERENCES relevance_gate_rules(id) ON DELETE SET NULL,
            reason      TEXT NOT NULL DEFAULT '',
            model       TEXT,
            mode        TEXT CHECK (mode IN ('per_window','per_message')),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (inbox_id)
        );
        CREATE INDEX relevance_verdicts_user_verdict
            ON relevance_verdicts (user_id, verdict);

        CREATE TABLE relevance_gate_settings (
            user_id    BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            enabled    BOOLEAN NOT NULL DEFAULT FALSE,
            mode       TEXT NOT NULL DEFAULT 'per_window'
                         CHECK (mode IN ('per_window','per_message')),
            model      TEXT NOT NULL DEFAULT 'claude-opus-4-8',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        ALTER TABLE work_item_failures
            DROP CONSTRAINT work_item_failures_stage_check;
        ALTER TABLE work_item_failures
            ADD CONSTRAINT work_item_failures_stage_check
            CHECK (stage IN ('summarize', 'extract', 'relevance'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM work_item_failures WHERE stage = 'relevance';
        ALTER TABLE work_item_failures
            DROP CONSTRAINT work_item_failures_stage_check;
        ALTER TABLE work_item_failures
            ADD CONSTRAINT work_item_failures_stage_check
            CHECK (stage IN ('summarize', 'extract'));

        DROP TABLE IF EXISTS relevance_verdicts CASCADE;
        DROP TABLE IF EXISTS relevance_gate_rules CASCADE;
        DROP TABLE IF EXISTS personal_interests CASCADE;
        DROP TABLE IF EXISTS relevance_gate_settings CASCADE;
        """
    )
