"""reglas del gate compuestas (remitente + patron) y bipolares (block + allow)

Revision ID: 0072_relevance_composite_rules
Revises: 0071_unify_relevance
Create Date: 2026-06-16

Rediseño del modelo de regla del gate (enmienda ADR-020). Dos cambios al esquema de
`relevance_gate_rules`:

1. POLARIDAD `effect` ('block' | 'allow'). Hasta hoy TODA regla era block (matchea →
   `not_relevant`); ahora una regla `allow` matchea → `relevant` determinista (el correo ENTRA
   sin pasar por el juez). Filas viejas → 'block' (default). `relevance_verdicts` NO cambia: el
   veredicto ('relevant'/'not_relevant') + el FK `rule_id` ya codifican la polaridad.

2. REGLA COMPUESTA: el predicado único (`kind`,`pattern`) se reemplaza por DOS slots nombrados
   que se combinan con AND — un remitente Y/O un patrón del asunto:
   - `sender_kind` ('sender_email'|'sender_domain'|'list_id') + `sender_value`: el QUIÉN.
   - `subject_pattern`: substring del asunto (case-insensitive): el QUÉ.
   Las reglas mineadas por el LLM llevan SIEMPRE los dos (el remitente solo es demasiado grueso;
   el patrón desambigua). El esquema solo exige ≥1 predicado, para no romper las reglas viejas de
   un solo criterio ni la creación manual «de a un remitente entero».

   Migración de filas existentes: `kind` ∈ {sender_email,sender_domain,list_id} → slot de
   remitente; `kind`='subject_contains' → `subject_pattern`. Luego se dropean `kind`/`pattern`.

3. El umbral de minería `mining_min_messages` baja su DEFAULT de 5 → 3 (la N configurable del
   disparador; las filas de settings existentes conservan su valor).

Numeración (migration-numbering-worktrees): 0072 verificado libre — head lineal = 0071, sin otros
worktrees/ramas con el número reservado (solo `main` + este worktree).

DOWNGRADE: best-effort y LOSSY. Reconstruye `kind`/`pattern` desde los slots (prefiere remitente;
si solo hay patrón → 'subject_contains'); las reglas que tengan AMBOS predicados (remitente +
asunto) NO caben en el modelo viejo de un solo criterio y se BORRAN. Es una salida de emergencia
de dev, no un round-trip de datos.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0072_relevance_composite_rules"
down_revision: str | None = "0071_unify_relevance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. effect (polaridad) + slots del predicado compuesto (nullable; se chequea ≥1 abajo)
    op.execute(
        """
        ALTER TABLE relevance_gate_rules
            ADD COLUMN effect TEXT NOT NULL DEFAULT 'block'
                CHECK (effect IN ('block','allow')),
            ADD COLUMN sender_kind TEXT
                CHECK (sender_kind IN ('sender_email','sender_domain','list_id')),
            ADD COLUMN sender_value TEXT,
            ADD COLUMN subject_pattern TEXT;
        """
    )

    # 2. Migrar el predicado único viejo a los slots nombrados
    op.execute(
        """
        UPDATE relevance_gate_rules
           SET sender_kind = kind, sender_value = pattern
         WHERE kind IN ('sender_email','sender_domain','list_id');

        UPDATE relevance_gate_rules
           SET subject_pattern = pattern
         WHERE kind = 'subject_contains';
        """
    )

    # 3. Soltar el UNIQUE viejo y las columnas viejas
    op.execute(
        """
        ALTER TABLE relevance_gate_rules
            DROP CONSTRAINT IF EXISTS relevance_gate_rules_user_id_kind_pattern_key;
        ALTER TABLE relevance_gate_rules
            DROP COLUMN kind,
            DROP COLUMN pattern;
        """
    )

    # 4. Invariantes del modelo compuesto + dedup robusto ante NULLs y mayúsculas
    op.execute(
        """
        ALTER TABLE relevance_gate_rules
            ADD CONSTRAINT relevance_gate_rules_has_predicate
                CHECK ((sender_kind IS NOT NULL AND sender_value IS NOT NULL)
                       OR subject_pattern IS NOT NULL),
            ADD CONSTRAINT relevance_gate_rules_sender_paired
                CHECK ((sender_kind IS NULL) = (sender_value IS NULL)),
            ADD CONSTRAINT relevance_gate_rules_sender_value_nonempty
                CHECK (sender_value IS NULL OR length(btrim(sender_value)) > 0),
            ADD CONSTRAINT relevance_gate_rules_subject_pattern_nonempty
                CHECK (subject_pattern IS NULL OR length(btrim(subject_pattern)) > 0);

        CREATE UNIQUE INDEX relevance_gate_rules_dedupe
            ON relevance_gate_rules (
                user_id, effect,
                lower(coalesce(sender_kind, '')),
                lower(coalesce(sender_value, '')),
                lower(coalesce(subject_pattern, ''))
            );
        """
    )

    # 5. N del disparador de minería: default 5 → 3 (las filas existentes conservan su valor)
    op.execute(
        """
        ALTER TABLE relevance_gate_settings
            ALTER COLUMN mining_min_messages SET DEFAULT 3;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE relevance_gate_settings
            ALTER COLUMN mining_min_messages SET DEFAULT 5;

        -- Las reglas con AMBOS predicados no caben en el modelo viejo de un solo criterio.
        DELETE FROM relevance_gate_rules
         WHERE sender_kind IS NOT NULL AND subject_pattern IS NOT NULL;

        DROP INDEX IF EXISTS relevance_gate_rules_dedupe;
        ALTER TABLE relevance_gate_rules
            DROP CONSTRAINT IF EXISTS relevance_gate_rules_has_predicate,
            DROP CONSTRAINT IF EXISTS relevance_gate_rules_sender_paired,
            DROP CONSTRAINT IF EXISTS relevance_gate_rules_sender_value_nonempty,
            DROP CONSTRAINT IF EXISTS relevance_gate_rules_subject_pattern_nonempty;

        ALTER TABLE relevance_gate_rules
            ADD COLUMN kind TEXT,
            ADD COLUMN pattern TEXT;

        UPDATE relevance_gate_rules
           SET kind = sender_kind, pattern = sender_value
         WHERE sender_kind IS NOT NULL;
        UPDATE relevance_gate_rules
           SET kind = 'subject_contains', pattern = subject_pattern
         WHERE sender_kind IS NULL AND subject_pattern IS NOT NULL;

        ALTER TABLE relevance_gate_rules
            ALTER COLUMN kind SET NOT NULL,
            ALTER COLUMN pattern SET NOT NULL,
            ADD CONSTRAINT relevance_gate_rules_kind_check
                CHECK (kind IN ('sender_email','sender_domain','subject_contains','list_id')),
            ADD CONSTRAINT relevance_gate_rules_pattern_check
                CHECK (length(btrim(pattern)) > 0),
            ADD CONSTRAINT relevance_gate_rules_user_id_kind_pattern_key
                UNIQUE (user_id, kind, pattern);

        ALTER TABLE relevance_gate_rules
            DROP COLUMN effect,
            DROP COLUMN sender_kind,
            DROP COLUMN sender_value,
            DROP COLUMN subject_pattern;
        """
    )
