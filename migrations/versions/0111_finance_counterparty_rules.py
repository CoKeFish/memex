"""Rubro por contraparte: lo que el dueño (o el modelo) dijo que es un comercio.

Revision ID: 0111_finance_counterparty_rules
Revises: 0110_asuntos_notas

«Otros» era el rubro más grande (726 de 1.890 pagos) por dos motivos: la lista de rubros era solo
de gastos (una transferencia a una persona o un traspaso entre cuentas propias no tenían dónde
caer) y, cuando el extracto trae solo el nombre del comercio, el modelo no tiene cómo saber qué
es. Lo segundo lo sabe el dueño: esta tabla guarda su palabra («Nacanisaje es un restaurante»),
o la clasificación del modelo en la pasada de reclasificación (`source='llm'`, que el dueño puede
pisar y nunca al revés). Se aplica a todos los movimientos de esa contraparte, pasados y futuros.

La llave es la contraparte normalizada (`memex_norm`, la misma de identidades).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0111_finance_counterparty_rules"
down_revision: str | None = "0110_asuntos_notas"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mod_finance_counterparty_rules (
            id                BIGSERIAL PRIMARY KEY,
            user_id           BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            counterparty_norm TEXT NOT NULL,
            counterparty      TEXT NOT NULL,
            category          TEXT NOT NULL,
            source            TEXT NOT NULL DEFAULT 'user' CHECK (source IN ('user', 'llm')),
            note              TEXT NOT NULL DEFAULT '',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, counterparty_norm)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE mod_finance_counterparty_rules")
