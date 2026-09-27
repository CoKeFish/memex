"""dead-letter de los workers LLM: contador de fallos por mensaje + 'pendiente de revisión'

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-31

Gap (c) de la auditoría de errores LLM. El cursor de summarize/extract es la AUSENCIA de fila
(en `summary_inbox_links` / `module_extractions`): un fallo NO deja cursor → el mensaje se
reintenta en cada corrida. Eso es correcto para fallos TRANSITORIOS (red/5xx/429, que ya
reintentan), pero un mensaje 'veneno' que falla SIEMPRE (p. ej. 400 content-filter, JSON que
nunca parsea, o una ventana que siempre trunca) se reintentaría para siempre, pagando una
llamada LLM cada vez, sin progreso.

`work_item_failures` lleva un contador de fallos por (stage, inbox_id). Al alcanzar el umbral
(memex.core.deadletter.MAX_WORK_ATTEMPTS=3) el item pasa a status='review' ('pendiente de
revisión') y los worksets lo EXCLUYEN: deja de reintentarse, SIN descartarse en silencio (queda
visible vía el router de revisión del API y recuperable con `requeue`).

Granularidad por inbox_id (no por módulo): el fallo se detecta a nivel VENTANA (una llamada LLM
por ventana), que no tiene identidad estable entre corridas; los inbox_id sí. Como una ventana
que falla se vuelve a formar igual, el contador converge. Limitación conocida: si un mensaje
veneno comparte ventana batch con mensajes sanos, estos acumulan fallos junto a él y caen a
'review' como daño colateral — por eso es 'pendiente de revisión' (no descarte) + `requeue`.
Precedente en el repo: `media_assets.ocr_attempts` + MAX_OCR_ATTEMPTS (el worker de OCR ya lo
hacía). El 402/saldo NO pasa por acá: aborta la corrida entera (LLMQuotaError), no se cuenta.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE work_item_failures (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            stage       TEXT NOT NULL CHECK (stage IN ('summarize', 'extract')),
            inbox_id    BIGINT NOT NULL REFERENCES inbox(id) ON DELETE CASCADE,
            attempts    INT NOT NULL DEFAULT 0,
            last_error  TEXT,
            status      TEXT NOT NULL DEFAULT 'failing'
                          CHECK (status IN ('failing', 'review')),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (stage, inbox_id)
        );
        CREATE INDEX work_item_failures_review
            ON work_item_failures (user_id, stage) WHERE status = 'review';
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS work_item_failures CASCADE;")
