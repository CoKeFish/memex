"""Notas del dueño sobre un asunto («contar algo»).

Revision ID: 0110_asuntos_notas
Revises: 0109_llm_deadline

Hasta acá la página de un asunto no tenía dónde contarle algo que no venía en un mensaje («me
dijeron que el viernes en la tarde puedo pasar»). El estado se reescribía a mano y los datos
sueltos iban a «A la mano», pero una nota no es ni lo uno ni lo otro: es un hito que dice el
dueño, y de ella pueden salir cambios (fecha a un paso, un paso hecho, un plazo, un estado).

La nota se guarda tal cual, con lo que el sistema entendió (`entendido`) y lo que cambió por
ella (`cambios`, lista de frases), así la historia del asunto la muestra y se puede auditar qué
hizo el intérprete. Sin FK a inbox: no nace de un mensaje.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0110_asuntos_notas"
down_revision: str | None = "0109_llm_deadline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mod_asuntos_notas (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            asunto_id   BIGINT NOT NULL REFERENCES mod_asuntos(id) ON DELETE CASCADE,
            texto       TEXT NOT NULL,
            entendido   TEXT NOT NULL DEFAULT '',
            cambios     JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX mod_asuntos_notas_asunto_idx ON mod_asuntos_notas (user_id, asunto_id, id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE mod_asuntos_notas")
