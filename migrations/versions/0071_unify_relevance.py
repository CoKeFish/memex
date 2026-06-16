"""relevancia unificada: candidatos por-procedimiento, absorber blacklist, sugerencias de interes

Revision ID: 0071_unify_relevance
Revises: 0070_sender_resolution_method
Create Date: 2026-06-16

Rediseño «un solo sistema de relevancia» (enmienda ADR-020). Esta migración prepara el esquema:

1. `relevance_candidates` deja de ser «cola del juez advisory» y pasa a ser la SALIDA de
   PROCEDIMIENTOS deterministas enchufables (uno = conteo de hechos; el diseño deja sumar otros).
   - `+ procedure` (qué procedimiento marcó la fila) y `+ unit_type` (seam por-ingestor: correo
     = 'sender'; a futuro 'topic'/'group'/'post_class'). Ambos TEXT libre (registro extensible,
     sin CHECK que obligue migración por cada procedimiento nuevo).
   - UNIQUE (user_id, sender_key) → (user_id, procedure, sender_key): varios procedimientos pueden
     marcar el mismo remitente de forma independiente.
   - `- llm_verdict`: el juez advisory se retira; la re-evaluación escribe el cursor canónico
     `relevance_verdicts` vía el MOTOR ÚNICO (no una nota aparte).

2. Absorber `sender_tier_overrides` (dirección ya fijada por el dueño: «absorber 'no procesar
   remitente' en las reglas del gate, el superset»). Las filas `tier='blacklist'` («no procesar»)
   se MIGRAN a `relevance_gate_rules` como reglas `sender_email` activas manuales; luego se borran
   y el CHECK se estrecha a ('batch','individual') — el override queda SOLO como dial de costo.

3. Nueva `interest_suggestions`: segundo lazo de feedback (rechazo manual → sugerir editar
   intereses). Espeja la auditoría de la minería de reglas: propone → el dueño acepta/ajusta;
   nunca auto-aplica.

4. Extender `relevance_gate_settings`: `mining_interleave` (minar entre lotes, default TRUE) e
   `interest_suggest_min_marks` (umbral del lazo de intereses, default 5).

Numeración (migration-numbering-worktrees): 0071 verificado libre — head lineal = 0070, solo `main`
sin otros worktrees con número reservado.

DOWNGRADE: restaura el ESQUEMA (re-ensancha CHECKs, re-agrega llm_verdict y el UNIQUE viejo, dropea
columnas/tabla nuevas). Es LOSSY: la migración de blacklist→reglas NO se revierte (las reglas
quedan); y si dos procedimientos marcaron el mismo `sender_key`, re-imponer el UNIQUE viejo
(user_id, sender_key) puede fallar — es una salida de emergencia de dev, no un round-trip de datos.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0071_unify_relevance"
down_revision: str | None = "0070_sender_resolution_method"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. relevance_candidates → salida de procedimientos enchufables
    op.execute(
        """
        ALTER TABLE relevance_candidates
            ADD COLUMN procedure TEXT NOT NULL DEFAULT 'sender_relevance',
            ADD COLUMN unit_type TEXT NOT NULL DEFAULT 'sender';
        ALTER TABLE relevance_candidates
            DROP CONSTRAINT IF EXISTS relevance_candidates_user_id_sender_key_key;
        ALTER TABLE relevance_candidates
            ADD CONSTRAINT relevance_candidates_user_id_procedure_sender_key_key
            UNIQUE (user_id, procedure, sender_key);
        ALTER TABLE relevance_candidates DROP COLUMN IF EXISTS llm_verdict;
        """
    )

    # 2. Absorber sender_tier_overrides: blacklist → regla sender_email; estrechar el dial de costo
    op.execute(
        """
        INSERT INTO relevance_gate_rules
            (user_id, kind, pattern, status, proposed_by, rationale, activated_at)
        SELECT user_id, 'sender_email', lower(btrim(sender_email)), 'active', 'manual',
               'migrado de sender_tier_overrides (no procesar remitente)', NOW()
        FROM sender_tier_overrides
        WHERE tier = 'blacklist' AND length(btrim(sender_email)) > 0
        ON CONFLICT (user_id, kind, pattern) DO NOTHING;

        DELETE FROM sender_tier_overrides WHERE tier = 'blacklist';

        ALTER TABLE sender_tier_overrides
            DROP CONSTRAINT IF EXISTS sender_tier_overrides_tier_check;
        ALTER TABLE sender_tier_overrides
            ADD CONSTRAINT sender_tier_overrides_tier_check
            CHECK (tier IN ('batch','individual'));
        """
    )

    # 3. interest_suggestions: segundo lazo (rechazo manual → sugerir intereses)
    op.execute(
        """
        CREATE TABLE interest_suggestions (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            action      TEXT NOT NULL CHECK (action IN ('add','remove')),
            text        TEXT NOT NULL CHECK (length(btrim(text)) > 0),
            interest_id BIGINT REFERENCES personal_interests(id) ON DELETE SET NULL,
            rationale   TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'proposed'
                          CHECK (status IN ('proposed','accepted','rejected')),
            proposed_by TEXT NOT NULL DEFAULT 'llm' CHECK (proposed_by IN ('llm','manual')),
            model       TEXT,
            evidence    JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at TIMESTAMPTZ
        );
        CREATE INDEX interest_suggestions_user_status
            ON interest_suggestions (user_id, status);
        -- Una sola propuesta pendiente por (acción, texto): la minería re-corre sin duplicar.
        CREATE UNIQUE INDEX interest_suggestions_pending_dedupe
            ON interest_suggestions (user_id, action, lower(text))
            WHERE status = 'proposed';
        """
    )

    # 4. Settings nuevos del sistema unificado
    op.execute(
        """
        ALTER TABLE relevance_gate_settings
            ADD COLUMN mining_interleave BOOLEAN NOT NULL DEFAULT TRUE,
            ADD COLUMN interest_suggest_min_marks INT NOT NULL DEFAULT 5
                CHECK (interest_suggest_min_marks >= 1);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE relevance_gate_settings
            DROP COLUMN IF EXISTS interest_suggest_min_marks,
            DROP COLUMN IF EXISTS mining_interleave;

        DROP TABLE IF EXISTS interest_suggestions CASCADE;

        ALTER TABLE sender_tier_overrides
            DROP CONSTRAINT IF EXISTS sender_tier_overrides_tier_check;
        ALTER TABLE sender_tier_overrides
            ADD CONSTRAINT sender_tier_overrides_tier_check
            CHECK (tier IN ('blacklist','batch','individual'));

        ALTER TABLE relevance_candidates ADD COLUMN IF NOT EXISTS llm_verdict JSONB;
        ALTER TABLE relevance_candidates
            DROP CONSTRAINT IF EXISTS relevance_candidates_user_id_procedure_sender_key_key;
        ALTER TABLE relevance_candidates
            ADD CONSTRAINT relevance_candidates_user_id_sender_key_key
            UNIQUE (user_id, sender_key);
        ALTER TABLE relevance_candidates
            DROP COLUMN IF EXISTS unit_type,
            DROP COLUMN IF EXISTS procedure;
        """
    )
