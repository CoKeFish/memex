"""finance: FK al catálogo de lugares (geo_places) en la transacción consolidada

Revision ID: 0063_finance_place
Revises: 0062_geo_places
Create Date: 2026-06-11

Mismo patrón que `mod_calendar_consolidated.place_id` (0062): el dominio referencia a geo por FK
al catálogo; geo no conoce dominios por nombre. Jerarquía de confianza del lugar de un pago: el
ping GPS en el momento del cobro (seam `memex-finance geo`) > asociación manual del agente
(`memex finance set-place`). El counterparty NO se geocodifica ("Rappi" es una cadena, no un
lugar): el texto libre solo entra al catálogo por pedido explícito.

`place` (TEXT) sigue siendo el texto extraído/visible; `place_id` es la referencia canónica.
ON DELETE SET NULL: borrar un lugar del catálogo no borra pagos, solo desreferencia.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0063_finance_place"
down_revision: str | None = "0062_geo_places"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_finance_consolidated
            ADD COLUMN place_id BIGINT REFERENCES geo_places(id) ON DELETE SET NULL;
        CREATE INDEX mod_finance_consolidated_place
            ON mod_finance_consolidated (place_id) WHERE place_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS mod_finance_consolidated_place;
        ALTER TABLE mod_finance_consolidated DROP COLUMN IF EXISTS place_id;
        """
    )
