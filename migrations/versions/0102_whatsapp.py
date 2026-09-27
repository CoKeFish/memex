"""WhatsApp como dispositivo vinculado: vínculo, catálogo de chats y bandeja de espera

Revision ID: 0102_whatsapp
Revises: 0101_finance_subscriptions
Create Date: 2026-09-13

WhatsApp entra como dispositivo vinculado (whatsmeow vía neonize): memex recibe TODO lo de la
cuenta, pero el dueño quiere solo algunos chats. Tres tablas lo sostienen:

- `whatsapp_links`: el vínculo por cuenta (`accounts` provider=whatsapp): teléfono, JID, estado del
  dispositivo y el último error. Una fila por cuenta.
- `whatsapp_chats`: el CATÁLOGO de chats visto por la sesión (nombre, tipo, cuántos mensajes y de
  qué fechas), para elegir cuáles entran desde el dashboard. La selección misma vive en
  `sources.config.allowed_chats` (mismo formato que Telegram) para que `chat_context` la lea igual.
- `whatsapp_buffer`: BANDEJA de los mensajes de chats NO incluidos (el historial inicial y lo que
  llega en vivo), guardados crudos y acotados por retención, para que incluir un chat más tarde
  traiga lo que ya pasó. whatsmeow no permite pedir historial viejo a demanda; esta bandeja es el
  margen para elegir sin apuro. Lo incluido NO pasa por acá: va directo al inbox.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0102_whatsapp"
down_revision: str | None = "0101_finance_subscriptions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE whatsapp_links (
            account_id    BIGINT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
            user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            phone         TEXT NOT NULL DEFAULT '',
            jid           TEXT,
            -- sin_vincular → codigo (esperando que el dueño escriba el código) → vinculado;
            -- desconectado (se cayó, reconecta solo) · cerrado (logout / desvinculado) ·
            -- baneado (WhatsApp suspendió el dispositivo).
            status        TEXT NOT NULL DEFAULT 'sin_vincular'
                            CHECK (status IN ('sin_vincular','codigo','vinculado','desconectado',
                                              'cerrado','baneado')),
            pair_code     TEXT,
            pair_code_at  TIMESTAMPTZ,
            linked_at     TIMESTAMPTZ,
            last_event_at TIMESTAMPTZ,
            last_error    TEXT,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE whatsapp_chats (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            account_id  BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            jid         TEXT NOT NULL,
            name        TEXT NOT NULL DEFAULT '',
            kind        TEXT NOT NULL DEFAULT 'user' CHECK (kind IN ('group','user','other')),
            msg_count   INTEGER NOT NULL DEFAULT 0,
            first_at    TIMESTAMPTZ,
            last_at     TIMESTAMPTZ,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (account_id, jid)
        );

        CREATE TABLE whatsapp_buffer (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            account_id  BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            chat_jid    TEXT NOT NULL,
            message_id  TEXT NOT NULL,
            from_me     BOOLEAN NOT NULL DEFAULT FALSE,
            sender      TEXT NOT NULL DEFAULT '',
            push_name   TEXT NOT NULL DEFAULT '',
            occurred_at TIMESTAMPTZ NOT NULL,
            -- `waE2E.Message` serializado (protobuf): se re-parsea al incluir el chat.
            raw         BYTEA NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (account_id, chat_jid, message_id)
        );
        CREATE INDEX whatsapp_buffer_chat ON whatsapp_buffer (account_id, chat_jid, occurred_at);
        CREATE INDEX whatsapp_buffer_received ON whatsapp_buffer (received_at);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS whatsapp_buffer;
        DROP TABLE IF EXISTS whatsapp_chats;
        DROP TABLE IF EXISTS whatsapp_links;
        """
    )
