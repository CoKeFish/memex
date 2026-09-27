"""identidades: event_id en menciones + kind 'platform_id' en identifiers

Revision ID: 0060_identidades_event_pid
Revises: 0059_calendar_manual_events
Create Date: 2026-06-11

(Revision id corto a propósito: `alembic_version.version_num` es VARCHAR(32).)

Dos piezas para que las identidades entren a la correlación determinista del grafo:

- `mod_identidades_mentions.event_id` (patrón 0038/0040): la mención que crea el cierre de un
  evento del agente (`memex start → identidad → end`) lleva el `event_id` del evento, y el brazo
  identidades de `_materialize_same_event` la teje con los hechos de finanzas/bienestar del MISMO
  evento. Hasta ahora la identidad registrada en un evento no correlacionaba ("fui al gym con
  Juan" no creaba arista Juan↔registro). TEXT nullable + índice (user_id, event_id), NO unique
  (una identidad puede avistarse en muchos eventos).

- `mod_identidades_identifiers.kind` admite `'platform_id'`: el id ESTABLE que asigna la
  plataforma (el `user_id` de Telegram). No es un `handle` (el username es mutable o ausente; el
  id no) y no se inventa un sentinel dentro de 'handle'. Lo escribe la creación determinista de
  identidades de remitentes de chat (`senders.py`) y lo consume la provenance derivada del
  grafo (brazo CHAT de `vertex_inbox_ids`). El CHECK de 0033 es inline sin nombre → autonombre de
  Postgres (mismo truco que 0057); queda con nombre explícito.

DOWNGRADE: borra los identifiers 'platform_id' ANTES de estrechar el CHECK (el ADD valida las
filas existentes); las identidades creadas por remitente quedan (son filas normales del
directorio) pero sin su identificador de plataforma — la provenance derivada deja de verlas.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0060_identidades_event_pid"
down_revision: str | None = "0059_calendar_manual_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. event_id de correlación en menciones (mismo patrón que 0038/0040).
    op.execute(
        """
        ALTER TABLE mod_identidades_mentions ADD COLUMN event_id TEXT;
        CREATE INDEX mod_identidades_mentions_user_event
            ON mod_identidades_mentions (user_id, event_id);
        """
    )
    # 2. 'platform_id' como kind de identificador.
    op.execute(
        """
        ALTER TABLE mod_identidades_identifiers
            DROP CONSTRAINT IF EXISTS mod_identidades_identifiers_kind_check;
        ALTER TABLE mod_identidades_identifiers
            ADD CONSTRAINT mod_identidades_identifiers_kind_check
            CHECK (kind IN ('email','phone','handle','domain','url','platform_id'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM mod_identidades_identifiers WHERE kind = 'platform_id';
        ALTER TABLE mod_identidades_identifiers
            DROP CONSTRAINT IF EXISTS mod_identidades_identifiers_kind_check;
        ALTER TABLE mod_identidades_identifiers
            ADD CONSTRAINT mod_identidades_identifiers_kind_check
            CHECK (kind IN ('email','phone','handle','domain','url'));
        DROP INDEX IF EXISTS mod_identidades_mentions_user_event;
        ALTER TABLE mod_identidades_mentions DROP COLUMN IF EXISTS event_id;
        """
    )
