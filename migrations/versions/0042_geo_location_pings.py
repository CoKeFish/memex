"""geo: geo_location_pings — pings GPS de ubicación (plano de datos del subsistema geo)

Tabla append-only multi-tenant para la ubicación del usuario. La app móvil manda los pings por el
gateway (`POST /gateway/location/pings`); de acá los leen los consumidores (LocationReader, el seam
LocationSource, el futuro clustering / daemon de transporte).

geo NO es un módulo de extracción, así que la tabla NO lleva prefijo `mod_` (sigue a la infra
no-módulo: sources, inbox, scheduler_settings). SIN dedup a propósito: no puede haber dos posiciones
del usuario en el mismo instante; cada ping se guarda tal cual.

Revision ID: 0042_geo_location_pings
Revises: 0041_agent_event_staging
Create Date: 2026-06-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0042_geo_location_pings"
down_revision: str | None = "0041_agent_event_staging"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE geo_location_pings (
            id           BIGSERIAL PRIMARY KEY,
            user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            lat          DOUBLE PRECISION NOT NULL,
            lng          DOUBLE PRECISION NOT NULL,
            accuracy_m   DOUBLE PRECISION,
            altitude_m   DOUBLE PRECISION,
            heading      DOUBLE PRECISION,
            speed_mps    DOUBLE PRECISION,
            captured_at  TIMESTAMPTZ NOT NULL,
            received_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            source       TEXT NOT NULL DEFAULT 'device'
                            CHECK (source IN ('device','manual','inferred')),
            metadata     JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        CREATE INDEX geo_location_pings_user_captured
            ON geo_location_pings (user_id, captured_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS geo_location_pings CASCADE;")
