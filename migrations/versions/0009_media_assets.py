"""media_assets — referencia a imágenes en MinIO + estado/texto de OCR

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-30

Primer slice del contrato de imágenes + OCR (backlog "Contrato de almacenamiento de imágenes
(MinIO) + OCR de todas las imágenes"). UNA tabla que es a la vez:

- LEDGER de referencia: el blob vive en MinIO (content-addressed); en la DB va SOLO la
  referencia (`object_key` + `bucket` + `sha256` + `content_type`/`size`), nunca el blob ni
  el secreto. Lo escribe el server al ingestar (en `ingest_batch`, junto al inbox, misma tx).
- ESTADO + TEXTO de OCR: la etapa aparte `memex-ocr` reclama las filas `pending`, OCR-ea con un
  proveedor de visión y escribe `ocr_text`/`ocr_status`. El render (summarizer + módulos) inyecta
  ese texto vía JOIN sobre `ocr_status='ok'`.

`ocr_status` es una máquina de estados (espeja `inbox.processed_at`): el worker reclama solo
`pending`; `error` es reintentable; `skipped` = no-OCR-able en este slice (PDF / oversize).

`UNIQUE (inbox_id, sha256)` = atribución de una imagen a un mensaje (espeja `summary_inbox_links`
/ `module_extractions`). El dedup del TRABAJO de OCR (misma imagen en dos mensajes → 1 sola llamada
de visión) lo hace el worker por `(user_id, sha256)`, reusando el texto ya OCR-eado.

FUERA DE ESTE SLICE: Telegram/social (este slice solo cubre adjuntos de email/IMAP). PDF se
captura y almacena pero queda `skipped` (los endpoints chat/visión OpenAI-compatible aceptan
imágenes vía image_url, no application/pdf). Rasterizar PDF queda en backlog.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE media_assets (
            id           BIGSERIAL PRIMARY KEY,
            user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            inbox_id     BIGINT NOT NULL REFERENCES inbox(id) ON DELETE CASCADE,
            sha256       TEXT   NOT NULL,
            object_key   TEXT   NOT NULL,
            bucket       TEXT   NOT NULL,
            content_type TEXT   NOT NULL,
            size_bytes   BIGINT NOT NULL,
            filename     TEXT,
            ocr_status   TEXT   NOT NULL DEFAULT 'pending'
                           CHECK (ocr_status IN ('pending','ok','error','skipped')),
            ocr_text     TEXT,
            ocr_model    TEXT,
            ocr_error    TEXT,
            ocr_attempts INT    NOT NULL DEFAULT 0,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ocr_done_at  TIMESTAMPTZ,
            UNIQUE (inbox_id, sha256)
        );
        CREATE INDEX media_assets_pending  ON media_assets (user_id, id) WHERE ocr_status = 'pending';
        CREATE INDEX media_assets_inbox    ON media_assets (inbox_id);
        CREATE INDEX media_assets_user_sha ON media_assets (user_id, sha256);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS media_assets CASCADE;")
