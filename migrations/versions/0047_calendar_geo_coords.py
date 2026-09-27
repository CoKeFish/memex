"""calendar: coordenadas geo del evento consolidado (geocoding del `location` texto → lat/lng)

`mod_calendar_consolidated.location` es texto libre (ej. "Aula 301", "Carrera 7 #45-23"). Para
ubicarlo en un mapa (y, a futuro, calcular reachability) se geocodifica con `memex.geo` y se guardan
las coordenadas acá, en columnas del evento consolidado (la fila canónica que lee el dominio).

`geo_geocoded_from` guarda el TEXTO exacto que se geocodificó: si el `location` cambia (p.ej. el paso
`merge` lo refina), el geocoding se rehace en la próxima consolidación; si no cambió, no se vuelve a
llamar a Maps (idempotente, controla gasto). Todas nullables: un evento sin lugar / virtual nunca se
geocodifica. NO hay tabla de caché aparte — el forward-geocode (dirección → coords) se materializa
una vez por evento acá; el `geo_place_cache` global solo cubre el reverse (coords → lugar).

Revision ID: 0047_calendar_geo_coords
Revises: 0043_geo_place_cache
Create Date: 2026-06-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0047_calendar_geo_coords"
down_revision: str | None = "0043_geo_place_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_calendar_consolidated
            ADD COLUMN geo_lat          DOUBLE PRECISION,
            ADD COLUMN geo_lng          DOUBLE PRECISION,
            ADD COLUMN geo_place_id     TEXT,
            ADD COLUMN geo_geocoded_from TEXT,
            ADD COLUMN geo_geocoded_at  TIMESTAMPTZ;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_calendar_consolidated
            DROP COLUMN IF EXISTS geo_lat,
            DROP COLUMN IF EXISTS geo_lng,
            DROP COLUMN IF EXISTS geo_place_id,
            DROP COLUMN IF EXISTS geo_geocoded_from,
            DROP COLUMN IF EXISTS geo_geocoded_at;
        """
    )
