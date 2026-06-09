"""calidad: item_count en module_extractions (señal de relevancia por mensaje)

Revision ID: 0048_module_item_count
Revises: 0047_calendar_geo_coords
Create Date: 2026-06-08

El cursor `module_extractions` marcaba solo "este módulo ya consideró este mensaje" — se escribe
también para módulos ruteados-fuera y para input vacío, así que NO distingue "produjo un hecho" de
"se consideró y no extrajo nada". `item_count` materializa cuántos hechos públicos produjo el módulo
PARA ESE mensaje (atribución por-mensaje vía `read_for_inbox`, NO el total de la ventana, que
sobre-atribuiría en lotes batch). Es la señal núcleo del sistema de calidad/relevancia: un mensaje
cuyo único módulo con `item_count>0` es `identidades` (o ninguno) no aportó hechos de dominio.

Default 0 (instantáneo en PG ≥11: default no-volátil, sin reescritura de tabla). El índice parcial
cubre el EXISTS "produjo hecho no-identidad" de la vista de relevancia. El backfill histórico lo hace
`memex-quality backfill-counts` (deriva el mismo conteo de las tablas de dominio), no esta migración.

`downgrade` dropea el índice + la columna.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0048_module_item_count"
down_revision: str | None = "0047_calendar_geo_coords"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE module_extractions ADD COLUMN item_count INT NOT NULL DEFAULT 0;")
    op.execute(
        "CREATE INDEX module_extractions_relevance ON module_extractions (inbox_id) "
        "WHERE module_slug <> 'identidades' AND item_count > 0;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS module_extractions_relevance;")
    op.execute("ALTER TABLE module_extractions DROP COLUMN IF EXISTS item_count;")
