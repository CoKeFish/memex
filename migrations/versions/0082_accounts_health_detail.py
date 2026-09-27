"""accounts: detalle y código del último health-check (para que la UI diga QUÉ falló y qué hacer)

Revision ID: 0082_accounts_health_detail
Revises: 0081_identifier_uniqueness
Create Date: 2026-09-01

Hasta acá `accounts` guardaba solo `health_status` + `last_health_check_at`: el texto del resultado
(`HealthResult.detail`) vivía únicamente en el toast del dashboard y se perdía al recargar. Con un
token OAuth caducado la tarjeta mostraba «healthy» + «google ✓» (el estado derivaba del último
ingestion_run, de meses atrás) y no había forma de saber ni por qué fallaba ni cómo reconectar.

- `health_detail`: texto legible del último chequeo (nunca secretos).
- `health_code`: código estable para que la UI ofrezca la acción correcta (hoy `reauth_required` =
  «reconectá con Google»; NULL = sin código).

Numeración (migration-numbering-worktrees): head lineal = 0081; el único worktree vivo
(relevancia-gate-regex) está en 0077 → 0082 libre.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0082_accounts_health_detail"
down_revision: str | None = "0081_identifier_uniqueness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE accounts ADD COLUMN health_detail TEXT")
    op.execute("ALTER TABLE accounts ADD COLUMN health_code TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE accounts DROP COLUMN health_code")
    op.execute("ALTER TABLE accounts DROP COLUMN health_detail")
