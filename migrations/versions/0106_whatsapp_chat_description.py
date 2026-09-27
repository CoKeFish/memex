"""WhatsApp: descripción del grupo en el catálogo.

Revision ID: 0106_whatsapp_chat_description
Revises: 0105_tareas_parent

La descripción (tema) de un grupo trae contenido útil —enlaces a carpetas con las diapositivas,
reglas, calendario— y no es un mensaje: llega con el historial (`Conversation.description`) y en vivo
por `get_group_info`. Se guarda en el catálogo y, si el chat está incluido, entra al inbox como un
mensaje propio del grupo (`whatsapp:<jid>:descripcion:<sha8>`), uno por versión de la descripción.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0106_whatsapp_chat_description"
down_revision: str | None = "0105_tareas_parent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE whatsapp_chats ADD COLUMN description TEXT NOT NULL DEFAULT ''")


def downgrade() -> None:
    op.execute("ALTER TABLE whatsapp_chats DROP COLUMN description")
