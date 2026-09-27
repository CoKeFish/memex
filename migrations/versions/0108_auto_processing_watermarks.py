"""Marca de agua POR MEDIO en el procesamiento automático.

Revision ID: 0108_auto_processing_watermarks
Revises: 0107_auto_processing

Con una sola marca compartida, sumar un medio nuevo lo dejaba mudo: el correo entró a `kinds` cuando
la marca ya había avanzado con el tráfico de los chats, así que 158 correos de días anteriores nunca
se miraron. Cada medio lleva ahora la suya (`watermarks`), empieza desde su propia ventana y no lo
arrastra el tráfico de otro.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0108_auto_processing_watermarks"
down_revision: str | None = "0107_auto_processing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE auto_processing ADD COLUMN watermarks JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    # La marca que había vale para los medios que ya estaban activos (lo que sigue de ahí es nuevo).
    op.execute(
        """
        UPDATE auto_processing SET watermarks = (
            SELECT COALESCE(jsonb_object_agg(k, to_jsonb(watermark)), '{}'::jsonb)
            FROM unnest(kinds) AS k
        )
        WHERE watermark IS NOT NULL
        """
    )
    op.execute("ALTER TABLE auto_processing DROP COLUMN watermark")


def downgrade() -> None:
    op.execute("ALTER TABLE auto_processing ADD COLUMN watermark TIMESTAMPTZ")
    op.execute(
        """
        UPDATE auto_processing SET watermark = (
            SELECT MIN((value #>> '{}')::timestamptz) FROM jsonb_each(watermarks)
        )
        """
    )
    op.execute("ALTER TABLE auto_processing DROP COLUMN watermarks")
