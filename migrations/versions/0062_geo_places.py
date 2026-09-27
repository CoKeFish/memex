"""geo: catálogo de lugares (geo_places) + caché de resolución por texto + FK desde calendario

Revision ID: 0062_geo_places
Revises: 0061_canales
Create Date: 2026-06-11

El dueño lleva registro de los LUGARES donde está/estuvo. Jerarquía de confianza: el ping GPS
(ingestor futuro) es la única fuente fiable de posición; un evento de calendario con lugar
físico = POSIBILIDAD de presencia. Patrón identidades: los DOMINIOS referencian a geo (FK al
catálogo), geo no conoce dominios por nombre — la correlación rica la teje el grafo de
relaciones.

- `geo_places` — catálogo canónico por usuario, single-writer `memex.geo.places`. La identidad
  fuerte la da `provider_place_id` (UNIQUE parcial): dos grafías del mismo lugar colapsan en UNA
  fila. `name` = cómo lo conoce el usuario (el PRIMER texto crudo que lo resolvió; editable a
  futuro). Un lugar sin coordenadas no es un lugar: ZERO_RESULTS vive en resolutions, no acá.
- `geo_place_resolutions` — caché texto→lugar POR USUARIO (≠ `geo_place_cache`, que es global por
  CELDA de coordenada para el reverse geocoding de pings). `query_norm` = normalize() del texto;
  mismo texto = 0 llamadas a Maps. `place_id NULL` = ZERO_RESULTS cacheado (no se reintenta).
- `mod_calendar_consolidated.place_id` — FK al catálogo. OJO con el homónimo: el `geo_place_id`
  TEXT existente (0047) es el place_id DEL PROVEEDOR (payload denormalizado del geocoding); este
  `place_id` BIGINT es la referencia interna al catálogo (convención `<entidad>_id`).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0062_geo_places"
down_revision: str | None = "0061_canales"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE geo_places (
            id                BIGSERIAL PRIMARY KEY,
            user_id           BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name              TEXT NOT NULL,
            formatted_address TEXT NOT NULL DEFAULT '',
            lat               DOUBLE PRECISION NOT NULL,
            lng               DOUBLE PRECISION NOT NULL,
            provider          TEXT NOT NULL DEFAULT 'google',
            provider_place_id TEXT,
            source            TEXT NOT NULL DEFAULT 'geocode',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        -- Colapso de grafías: dos textos que resuelven al mismo place_id del proveedor = UN lugar.
        CREATE UNIQUE INDEX geo_places_user_provider_pid
            ON geo_places (user_id, provider, provider_place_id)
            WHERE provider_place_id IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE TABLE geo_place_resolutions (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            query_norm  TEXT NOT NULL,
            place_id    BIGINT REFERENCES geo_places(id) ON DELETE CASCADE,
            resolved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, query_norm)
        );
        CREATE INDEX geo_place_resolutions_place
            ON geo_place_resolutions (place_id) WHERE place_id IS NOT NULL;
        """
    )
    op.execute(
        """
        ALTER TABLE mod_calendar_consolidated
            ADD COLUMN place_id BIGINT REFERENCES geo_places(id) ON DELETE SET NULL;
        CREATE INDEX mod_calendar_consolidated_place
            ON mod_calendar_consolidated (place_id) WHERE place_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS mod_calendar_consolidated_place;
        ALTER TABLE mod_calendar_consolidated DROP COLUMN IF EXISTS place_id;
        """
    )
    op.execute("DROP TABLE IF EXISTS geo_place_resolutions CASCADE;")
    op.execute("DROP TABLE IF EXISTS geo_places CASCADE;")
