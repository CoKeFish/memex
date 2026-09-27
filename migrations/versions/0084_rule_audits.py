"""Auditoría de reglas del gate: segunda opinión del juez por muestreo + contadores por regla

Revision ID: 0084_rule_audits
Revises: 0083_code_version_stamps
Create Date: 2026-09-04

Una regla es una decisión permanente sobre un remitente, pero los remitentes cambian lo que mandan
(Kronnect: assets en junio, escaparate de juegos en septiembre). Sin auditoría, una regla envejecida
solo se descubre abriendo un correo por casualidad. Ver `memex.relevance.audit`.

- `relevance_gate_settings.audit_sample_every`: 1 de cada N decisiones por regla pasa igual por el
  juez (0 = apagado; el 1.º hit siempre).
- `relevance_gate_rules`: `hit_count`/`last_hit_at` (cuánto decide la regla), `audit_count`,
  `contradictions` (juez en desacuerdo desde la última revisión), `last_confirmed_at`,
  `last_contradiction_at`, `reviewed_at`. Arrancan en 0: el PRÓXIMO correo de cada regla existente
  se audita (estreno rodante, sin backfill).
- `relevance_rule_audits`: la opinión del juez por (regla, correo), sin tocar el veredicto.

Numeración (migration-numbering-worktrees): head lineal = 0083; el único otro worktree vivo
(relevancia-gate-regex) está en 0077 → 0084 libre.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0084_rule_audits"
down_revision: str | None = "0083_code_version_stamps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE relevance_gate_settings "
        "ADD COLUMN audit_sample_every INTEGER NOT NULL DEFAULT 10 "
        "CONSTRAINT ck_gate_settings_audit_sample CHECK (audit_sample_every >= 0)"
    )
    op.execute(
        """
        ALTER TABLE relevance_gate_rules
            ADD COLUMN hit_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN last_hit_at TIMESTAMPTZ,
            ADD COLUMN audit_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN contradictions INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN last_confirmed_at TIMESTAMPTZ,
            ADD COLUMN last_contradiction_at TIMESTAMPTZ,
            ADD COLUMN reviewed_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        CREATE TABLE relevance_rule_audits (
            id            BIGSERIAL PRIMARY KEY,
            user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            rule_id       BIGINT NOT NULL REFERENCES relevance_gate_rules(id) ON DELETE CASCADE,
            inbox_id      BIGINT NOT NULL REFERENCES inbox(id) ON DELETE CASCADE,
            rule_verdict  TEXT NOT NULL,
            judge_verdict TEXT NOT NULL,
            judge_reason  TEXT NOT NULL DEFAULT '',
            model         TEXT,
            agrees        BOOLEAN,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (rule_id, inbox_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_rule_audits_user_rule "
        "ON relevance_rule_audits (user_id, rule_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE relevance_rule_audits")
    op.execute(
        """
        ALTER TABLE relevance_gate_rules
            DROP COLUMN reviewed_at, DROP COLUMN last_contradiction_at,
            DROP COLUMN last_confirmed_at, DROP COLUMN contradictions,
            DROP COLUMN audit_count, DROP COLUMN last_hit_at, DROP COLUMN hit_count
        """
    )
    op.execute("ALTER TABLE relevance_gate_settings DROP COLUMN audit_sample_every")
