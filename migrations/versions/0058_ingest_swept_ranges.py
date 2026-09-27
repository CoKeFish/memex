"""ingest_swept_ranges: bitácora append-only de rangos de fechas ya BARRIDOS por la ingesta

Revision ID: 0058_ingest_swept_ranges
Revises: 0057_identidades_kind_producto
Create Date: 2026-06-10

Alimenta el overlay de "barrido" del timeline de cobertura (GET /inbox/coverage): un rango barrido
dice "este tramo de fechas de origen ya se ingirió, haya dejado mensajes o no" — distingue
"barrí y estaba vacío" de "nunca lo intenté". Se escribe desde el seam compartido
`run_fetch_window` (mode=range con ventana cerrada, sin dry-run, sin truncamiento por límite),
así cubre el backfill segmentado Y el fetch por rango manual con el mismo código.

Append-only a propósito: reconfigurar o borrar el `backfill_job` NO borra lo ya barrido (el
objetivo del timeline es saber qué se ingirió). El dedup/solape lo resuelve el lector fundiendo
intervalos. `range_end` EXCLUSIVO (misma convención que backfill_jobs / IMAP BEFORE).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0058_ingest_swept_ranges"
down_revision: str | None = "0057_identidades_kind_producto"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ingest_swept_ranges (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
            source_id   BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            range_start DATE NOT NULL,
            range_end   DATE NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT ingest_swept_ranges_range_ck CHECK (range_end > range_start)
        );
        """
    )
    op.execute(
        "CREATE INDEX ingest_swept_ranges_user_source "
        "ON ingest_swept_ranges (user_id, source_id, range_start);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ingest_swept_ranges CASCADE;")
