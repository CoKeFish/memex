"""Tope de reloj de pared por llamada, configurable por consumidor.

Revision ID: 0109_llm_deadline
Revises: 0108_auto_processing_watermarks

El timeout de lectura mide el hueco ENTRE bytes, y un proveedor que gotea keep-alive lo reinicia
para siempre: el 2026-09-14 DeepSeek aceptó un request, devolvió 200 en las cabeceras y tardó 15
minutos en mandar un cuerpo vacío, con el job de procesamiento clavado todo ese rato. El tope nuevo
es de reloj de pared y corta la llamada entera.

El default (180 s) vive en el código; esta columna es para el consumidor que tarda de verdad.
`asuntos_assign` usa un modelo de razonamiento y sus llamadas llegan legítimamente a ~500 s: se le
deja 900 s para que el tope no le corte trabajo bueno.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0109_llm_deadline"
down_revision: str | None = "0108_auto_processing_watermarks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE llm_consumer_settings ADD COLUMN deadline_s DOUBLE PRECISION")
    op.execute(
        "ALTER TABLE llm_consumer_settings ADD CONSTRAINT llm_consumer_settings_deadline_check "
        "CHECK (deadline_s IS NULL OR deadline_s > 0)"
    )
    op.execute(
        "UPDATE llm_consumer_settings SET deadline_s = 900 WHERE consumer = 'asuntos_assign'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE llm_consumer_settings DROP COLUMN deadline_s")
