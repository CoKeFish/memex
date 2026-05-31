"""calendar como dominio bidireccional: sync de proveedores + consolidación + write-back

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-30

Convierte el módulo `calendar` (que hoy solo extrae fechas de mensajes vía LLM, slice 2 /
migración 0010) en un DOMINIO de calendario completo: ingresa eventos de proveedores externos
(Google, luego Outlook), los consolida con dedup en 2 fases, aplica prioridad aportada por otros
módulos, y escribe de vuelta a los proveedores (write-back) propagando entre ellos. Enmienda de
ADR-015 §4 (el sync de proveedor corre DENTRO del módulo, NO como ingestor; los eventos del
proveedor ya vienen estructurados y NO pasan por inbox/classifier/LLM).

Todo el DDL es ADITIVO (columnas nullable / con default, tablas nuevas) → no rompe lo existente.
Se crea todo en esta migración aunque el roadmap lo entregue por slices, igual que 0010
forward-declaró tablas. Qué USA cada slice:

- SLICE 1 (ingress read-only idempotente): columnas de proveedor + estado de procesamiento en
  `mod_calendar_events` (origin/provider/provider_event_id/etag/updated/status/manual/
  processed_at/processing_outcome) + el índice UNIQUE parcial (idempotencia de sync) +
  `mod_calendar_provider_accounts` (cuenta + cursor delta `sync_token`) + `mod_calendar_sync_runs`
  y `mod_calendar_event_changes` (observabilidad: created/modified/deleted, requisito del dueño).
- SLICE 2 (dedup FASE 2 LLM): columnas `decided_by`/`confidence`/`rationale`/`decided_at` en
  `mod_calendar_dedup_candidates`.
- SLICE 3 (consolidación): `mod_calendar_consolidated` + `mod_calendar_event_links`.
- SLICE 4 (prioridad + conflictos): columnas `priority_rank`/`protected`/`override_policy`/
  `contributed_by` en `mod_calendar_events` + `mod_calendar_conflicts` (cola "pendiente de
  revisión": dos eventos DISTINTOS que se solapan y ambos importan; NUNCA fusiona ni descarta).
- SLICE 5 (write-back): `mod_calendar_writeback` (estado por (consolidado, cuenta) +
  `last_pushed_etag` para echo-suppression del loop).

SECRETOS FUERA DE LA DB (ADR-015 §7 / ADR-001): `mod_calendar_provider_accounts.token_path_env`
guarda el NOMBRE de la env var que apunta al archivo del token OAuth (igual que
`imap/config.py:oauth_token_path_env`), NUNCA el refresh/access token. `sync_token` es un cursor
opaco de Google (no es secreto) → ese SÍ va en la DB.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Cuentas de proveedor + cursor de sync incremental. Referencia al secreto, no el secreto.
    op.execute(
        """
        CREATE TABLE mod_calendar_provider_accounts (
            id             BIGSERIAL PRIMARY KEY,
            user_id        BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider       TEXT NOT NULL,
            account_label  TEXT NOT NULL,
            calendar_id    TEXT NOT NULL DEFAULT 'primary',
            token_path_env TEXT NOT NULL,
            sync_token     TEXT,
            last_sync_at   TIMESTAMPTZ,
            enabled        BOOLEAN NOT NULL DEFAULT TRUE,
            write_back     BOOLEAN NOT NULL DEFAULT FALSE,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, provider, account_label, calendar_id)
        );
        CREATE INDEX mod_calendar_provider_accounts_user
            ON mod_calendar_provider_accounts (user_id) WHERE enabled;
        """
    )

    # 2. Eventos crudos: origen + idempotencia de proveedor + prioridad + estado de procesamiento.
    op.execute(
        """
        ALTER TABLE mod_calendar_events
            ADD COLUMN origin              TEXT NOT NULL DEFAULT 'extraction'
                                            CHECK (origin IN ('extraction','provider','module')),
            ADD COLUMN provider            TEXT,
            ADD COLUMN provider_account_id BIGINT
                                            REFERENCES mod_calendar_provider_accounts(id)
                                            ON DELETE SET NULL,
            ADD COLUMN provider_event_id   TEXT,
            ADD COLUMN provider_etag       TEXT,
            ADD COLUMN provider_updated    TIMESTAMPTZ,
            ADD COLUMN provider_status     TEXT,
            ADD COLUMN manual              BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN priority_rank       INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN protected           BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN override_policy     TEXT NOT NULL DEFAULT 'replace'
                                            CHECK (override_policy IN ('replace','fill_only')),
            ADD COLUMN contributed_by      TEXT,
            ADD COLUMN processed_at        TIMESTAMPTZ,
            ADD COLUMN processing_outcome  TEXT NOT NULL DEFAULT 'pending'
                                            CHECK (processing_outcome IN
                                              ('pending','unique','duplicate','shadowed',
                                               'conflict','echo'));
        -- Idempotencia: un eventId de proveedor no se duplica entre corridas de sync.
        CREATE UNIQUE INDEX mod_calendar_events_provider_uniq
            ON mod_calendar_events (provider, provider_account_id, provider_event_id)
            WHERE provider_event_id IS NOT NULL;
        -- Listar pendientes / en conflicto barato.
        CREATE INDEX mod_calendar_events_user_outcome
            ON mod_calendar_events (user_id, processing_outcome);
        """
    )

    # 3. Consolidación (slice 3): evento canónico + links N:1 hacia los crudos.
    op.execute(
        """
        CREATE TABLE mod_calendar_consolidated (
            id              BIGSERIAL PRIMARY KEY,
            user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title           TEXT NOT NULL,
            starts_on       DATE NOT NULL,
            ends_on         DATE,
            start_time      TIME,
            end_time        TIME,
            location        TEXT NOT NULL DEFAULT '',
            description     TEXT NOT NULL DEFAULT '',
            winner_event_id BIGINT REFERENCES mod_calendar_events(id) ON DELETE SET NULL,
            deleted         BOOLEAN NOT NULL DEFAULT FALSE,
            merge_signature TEXT,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX mod_calendar_consolidated_user_starts
            ON mod_calendar_consolidated (user_id, starts_on);

        CREATE TABLE mod_calendar_event_links (
            id              BIGSERIAL PRIMARY KEY,
            user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            consolidated_id BIGINT NOT NULL
                              REFERENCES mod_calendar_consolidated(id) ON DELETE CASCADE,
            event_id        BIGINT NOT NULL REFERENCES mod_calendar_events(id) ON DELETE CASCADE,
            UNIQUE (event_id)
        );
        CREATE INDEX mod_calendar_event_links_consolidated
            ON mod_calendar_event_links (consolidated_id);
        """
    )

    # 4. Write-back / inter-proveedor (slice 5): estado + echo-suppression del loop.
    op.execute(
        """
        CREATE TABLE mod_calendar_writeback (
            id                  BIGSERIAL PRIMARY KEY,
            user_id             BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            consolidated_id     BIGINT NOT NULL
                                  REFERENCES mod_calendar_consolidated(id) ON DELETE CASCADE,
            provider_account_id BIGINT NOT NULL
                                  REFERENCES mod_calendar_provider_accounts(id) ON DELETE CASCADE,
            provider_event_id   TEXT,
            last_pushed_etag    TEXT,
            last_pushed_signature TEXT,
            last_pushed_at      TIMESTAMPTZ,
            state               TEXT NOT NULL DEFAULT 'pending'
                                  CHECK (state IN ('pending','synced','deleted','error')),
            error               TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (consolidated_id, provider_account_id)
        );
        """
    )

    # 5. Observabilidad (slice 1): corrida de sync + auditoría por-evento (created/modified/deleted).
    op.execute(
        """
        CREATE TABLE mod_calendar_sync_runs (
            id                  BIGSERIAL PRIMARY KEY,
            user_id             BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider_account_id BIGINT
                                  REFERENCES mod_calendar_provider_accounts(id) ON DELETE SET NULL,
            direction           TEXT NOT NULL CHECK (direction IN ('ingress','egress')),
            pulled              INTEGER NOT NULL DEFAULT 0,
            created             INTEGER NOT NULL DEFAULT 0,
            modified            INTEGER NOT NULL DEFAULT 0,
            deleted             INTEGER NOT NULL DEFAULT 0,
            unchanged           INTEGER NOT NULL DEFAULT 0,
            dedup_pairs         INTEGER NOT NULL DEFAULT 0,
            errors              INTEGER NOT NULL DEFAULT 0,
            status              TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','error')),
            detail              JSONB NOT NULL DEFAULT '{}'::jsonb,
            started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at         TIMESTAMPTZ
        );
        CREATE INDEX mod_calendar_sync_runs_user_started
            ON mod_calendar_sync_runs (user_id, started_at DESC);

        CREATE TABLE mod_calendar_event_changes (
            id                BIGSERIAL PRIMARY KEY,
            user_id           BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            sync_run_id       BIGINT REFERENCES mod_calendar_sync_runs(id) ON DELETE SET NULL,
            event_id          BIGINT REFERENCES mod_calendar_events(id) ON DELETE SET NULL,
            consolidated_id   BIGINT REFERENCES mod_calendar_consolidated(id) ON DELETE SET NULL,
            provider          TEXT,
            provider_event_id TEXT,
            direction         TEXT NOT NULL CHECK (direction IN ('ingress','egress')),
            action            TEXT NOT NULL CHECK (action IN ('created','modified','deleted')),
            detail            JSONB NOT NULL DEFAULT '{}'::jsonb,
            at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX mod_calendar_event_changes_run
            ON mod_calendar_event_changes (sync_run_id);
        CREATE INDEX mod_calendar_event_changes_user_at
            ON mod_calendar_event_changes (user_id, at DESC);
        """
    )

    # 6. Auditoría del dedup FASE 2 LLM (slice 2): cómo se decidió cada par candidato.
    op.execute(
        """
        ALTER TABLE mod_calendar_dedup_candidates
            ADD COLUMN decided_by TEXT,
            ADD COLUMN confidence NUMERIC(4,3),
            ADD COLUMN rationale  TEXT,
            ADD COLUMN decided_at TIMESTAMPTZ;
        """
    )

    # 7. Conflictos (slice 4): pares de eventos CONSOLIDADOS DISTINTOS que se solapan en el tiempo
    # y ambos son de alta importancia (NO duplicados — eso ya lo resolvió la consolidación). Cola
    # "pendiente de revisión": NUNCA fusiona ni descarta, solo encola para decisión humana.
    op.execute(
        """
        CREATE TABLE mod_calendar_conflicts (
            id               BIGSERIAL PRIMARY KEY,
            user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            consolidated_a_id BIGINT NOT NULL
                               REFERENCES mod_calendar_consolidated(id) ON DELETE CASCADE,
            consolidated_b_id BIGINT NOT NULL
                               REFERENCES mod_calendar_consolidated(id) ON DELETE CASCADE,
            reason           TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'pending'
                               CHECK (status IN ('pending','resolved','dismissed')),
            resolved_at      TIMESTAMPTZ,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (consolidated_a_id < consolidated_b_id),
            UNIQUE (consolidated_a_id, consolidated_b_id)
        );
        CREATE INDEX mod_calendar_conflicts_user_status
            ON mod_calendar_conflicts (user_id, status);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mod_calendar_conflicts CASCADE;")
    op.execute(
        """
        ALTER TABLE mod_calendar_dedup_candidates
            DROP COLUMN IF EXISTS decided_by,
            DROP COLUMN IF EXISTS confidence,
            DROP COLUMN IF EXISTS rationale,
            DROP COLUMN IF EXISTS decided_at;
        """
    )
    op.execute("DROP TABLE IF EXISTS mod_calendar_event_changes CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_calendar_sync_runs CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_calendar_writeback CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_calendar_event_links CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_calendar_consolidated CASCADE;")
    op.execute(
        """
        ALTER TABLE mod_calendar_events
            DROP COLUMN IF EXISTS processing_outcome,
            DROP COLUMN IF EXISTS processed_at,
            DROP COLUMN IF EXISTS contributed_by,
            DROP COLUMN IF EXISTS override_policy,
            DROP COLUMN IF EXISTS protected,
            DROP COLUMN IF EXISTS priority_rank,
            DROP COLUMN IF EXISTS manual,
            DROP COLUMN IF EXISTS provider_status,
            DROP COLUMN IF EXISTS provider_updated,
            DROP COLUMN IF EXISTS provider_etag,
            DROP COLUMN IF EXISTS provider_event_id,
            DROP COLUMN IF EXISTS provider_account_id,
            DROP COLUMN IF EXISTS provider,
            DROP COLUMN IF EXISTS origin;
        """
    )
    op.execute("DROP TABLE IF EXISTS mod_calendar_provider_accounts CASCADE;")
