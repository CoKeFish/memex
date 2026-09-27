"""reglas del gate: el patrón pasa a REGEX sobre asunto/cuerpo (`pattern` + `match_field`)

Revision ID: 0077_relevance_regex_rules
Revises: 0076_calendar_participants
Create Date: 2026-06-19

Rediseño del predicado de patrón de `relevance_gate_rules` (enmienda ADR-020). Hasta hoy el patrón
era `subject_pattern` = SUBSTRING case-insensitive SOLO sobre el asunto — un footgun estructural
(un substring corto matchea dentro de palabras: la regla `off` bloqueó `official`) y de baja
cobertura (no captura remitentes cuyo asunto varía pero el cuerpo repite). Ahora:

1. `subject_pattern` → `pattern`: el patrón es un REGEX (dialecto restringido que coincide en Python
   `re` y Postgres ARE; ver `relevance/rules.py`).
2. `match_field` ('subject'|'body'|'subject_or_body'): contra qué se aplica el regex. Pareado con
   `pattern` (ambos NULL o ambos no-NULL).

SIN compatibilidad (decisión del dueño): las reglas viejas (substring) NO se convierten. La
migración BORRA las filas de `relevance_gate_rules` — es una reset estructural inherente al cambio
breaking; las reglas se re-crean a mano 1:1 como regex por el camino validado (CLI/dry-run). Los
`relevance_verdicts` ya emitidos sobreviven (FK `rule_id` ON DELETE SET NULL).

El índice único `relevance_gate_rules_dedupe` se reconstruye incluyendo `coalesce(pattern,'')` (SIN
`lower`: el case del regex es significativo, `\\D` ≠ `\\d`) y `coalesce(match_field,'')`. DEBE
coincidir EXACTO con el `ON CONFLICT` de `create_rule` (rules.py) — lockstep.

Numeración (migration-numbering-worktrees): 0077 verificado libre en main + worktrees
(relevancia-unificada, coocurrencia-densos-all-type); head lineal = 0076_calendar_participants.

DOWNGRADE: dev, simple. Revierte el esquema (drop match_field + checks, rename `pattern` →
`subject_pattern`, recrea el índice y el CHECK viejos). No re-crea reglas ni hace round-trip de datos.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0077_relevance_regex_rules"
down_revision: str | None = "0076_calendar_participants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        -- SIN compat: las reglas viejas (substring) se borran y se re-crean a mano como regex.
        DELETE FROM relevance_gate_rules;

        DROP INDEX IF EXISTS relevance_gate_rules_dedupe;

        ALTER TABLE relevance_gate_rules RENAME COLUMN subject_pattern TO pattern;

        ALTER TABLE relevance_gate_rules
            ADD COLUMN match_field TEXT
                CHECK (match_field IS NULL OR match_field IN ('subject','body','subject_or_body')),
            ADD CONSTRAINT relevance_gate_rules_pattern_field_paired
                CHECK ((pattern IS NULL) = (match_field IS NULL));

        -- El CHECK nonempty de 0072 quedó con nombre de la columna vieja; renombrarlo (cosmético).
        ALTER TABLE relevance_gate_rules
            DROP CONSTRAINT IF EXISTS relevance_gate_rules_subject_pattern_nonempty,
            ADD CONSTRAINT relevance_gate_rules_pattern_nonempty
                CHECK (pattern IS NULL OR length(btrim(pattern)) > 0);

        -- Dedup robusto: pattern SIN lower (el regex es case-sensitive), match_field en la llave.
        -- Espejo EXACTO del ON CONFLICT de create_rule (rules.py).
        CREATE UNIQUE INDEX relevance_gate_rules_dedupe
            ON relevance_gate_rules (
                user_id, effect,
                lower(coalesce(sender_kind, '')),
                lower(coalesce(sender_value, '')),
                coalesce(pattern, ''),
                coalesce(match_field, '')
            );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS relevance_gate_rules_dedupe;

        ALTER TABLE relevance_gate_rules
            DROP CONSTRAINT IF EXISTS relevance_gate_rules_pattern_field_paired,
            DROP CONSTRAINT IF EXISTS relevance_gate_rules_pattern_nonempty,
            DROP COLUMN match_field;

        ALTER TABLE relevance_gate_rules RENAME COLUMN pattern TO subject_pattern;

        ALTER TABLE relevance_gate_rules
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
