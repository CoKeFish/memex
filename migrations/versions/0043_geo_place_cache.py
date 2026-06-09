"""geo: geo_place_cache — caché de resoluciones coordenada → lugar (reverse geocoding + POI)

`resolve_place` llama a Maps (cuesta), así que cachea acá su resultado por CELDA de coordenada para no
re-llamar. Es dato de REFERENCIA, igual para todos los usuarios → la tabla es **global, sin `user_id`**
(misma excepción justificada que una tabla de tarifas). La celda = lat/lng redondeados (ver
`geo/store.py`, `_CELL_PRECISION`). `types` JSONB (como el resto de campos flexibles del repo).

Revision ID: 0043_geo_place_cache
Revises: 0042_geo_location_pings
Create Date: 2026-06-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0043_geo_place_cache"
down_revision: str | None = "0042_geo_location_pings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE geo_place_cache (
            id                BIGSERIAL PRIMARY KEY,
            cell_lat          DOUBLE PRECISION NOT NULL,
            cell_lng          DOUBLE PRECISION NOT NULL,
            lat               DOUBLE PRECISION NOT NULL,
            lng               DOUBLE PRECISION NOT NULL,
            name              TEXT,
            formatted_address TEXT NOT NULL DEFAULT '',
            place_id          TEXT,
            types             JSONB NOT NULL DEFAULT '[]'::jsonb,
            resolved_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (cell_lat, cell_lng)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS geo_place_cache CASCADE;")
