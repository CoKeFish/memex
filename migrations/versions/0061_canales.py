"""mod_canales: el CANAL de chat como entidad del grafo

Revision ID: 0061_canales
Revises: 0060_identidades_event_pid
Create Date: 2026-06-11

Los chats del usuario son contextos concretos (grupos curados por allowlist): el canal es la
entidad que ata ese contexto. Hasta ahora vivía solo en el payload (`chat_id`/`chat_title`) y el
contenido extraído de un chat quedaba suelto en el grafo. La tabla es del GRAFO (como
`relation_clusters`): la deriva `relations/canales.sync_canales` de los payloads de inbox
(full-sweep idempotente, upsert por identidad natural `(user, platform, external_id)` — sin
`source_id`: dos fuentes podrían pullear el mismo chat). Se proyecta como vértice `canal`
(NODE_SOURCES) y recibe: provenance derivada (co-ocurrencia con los vértices de cada mensaje del
chat) + aristas confirmadas `participa_en` persona→canal.

`chat_kind` sin CHECK a propósito (group|supergroup|channel hoy; otras plataformas mañana).
`metadata` puede absorber `topic_id` si algún día los topics son sub-canales (cúmulos jerárquicos
no necesitan nada estructural acá: las aristas son genéricas).

El índice por expresión sobre inbox acelera los JOIN payload→canal del build (sync, provenance y
participa_en). El predicado del índice parcial es EXACTAMENTE `payload->>'chat_id' IS NOT NULL`
— las queries deben usar ese mismo predicado (no el operador `?`) para que el planner lo use.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0061_canales"
down_revision: str | None = "0060_identidades_event_pid"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mod_canales (
            id           BIGSERIAL PRIMARY KEY,
            user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            platform     TEXT NOT NULL,
            external_id  TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            chat_kind    TEXT,
            metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, platform, external_id)
        );
        CREATE INDEX inbox_payload_chat_id ON inbox ((payload->>'chat_id'))
            WHERE payload->>'chat_id' IS NOT NULL;
        """
    )


def downgrade() -> None:
    # Las aristas que tocaban vértices 'canal' quedan huérfanas; la poda del build las barre.
    op.execute(
        """
        DROP INDEX IF EXISTS inbox_payload_chat_id;
        DROP TABLE IF EXISTS mod_canales;
        """
    )
