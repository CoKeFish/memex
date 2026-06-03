"""módulo identidades (ADR-015, slice 1: directorio + extracción): mod_identidades_*

Revision ID: 0027_identidades
Revises: 0026
Create Date: 2026-06-02

Storage del módulo `identidades` (patrón `mod_<slug>_*`, ADR-015 §6). El Design Doc
"Relaciones entre dominios — átomos, entidades y enlaces" reserva este módulo como
"Contactos / Personas": molécula Persona/Organización, hogar del átomo "agente",
referenciado por muchos. Modela TRES altitudes del dato:

  menciones (crudo por-mensaje) → entidades canónicas (persona/org) → (futuro) aristas

- `mod_identidades_provider_accounts` — cuenta Google Contacts + cursor delta `sync_token`.
  A diferencia de calendar (que guarda `token_path_env`, una referencia al token en disco),
  acá `account_id` apunta a la fila `accounts` del dashboard cuyo VAULT (`account_secrets`,
  migración 0019) tiene el `google_oauth_token` cifrado (Decisión 6: un único token Google para
  todo memex). `sync_token` es un cursor opaco de la People API — no es secreto → va en la DB.
- `mod_identidades_orgs` — organizaciones / productos / agentes (la lista manual de interés:
  Unity, Claude, …). `kind` distingue el sub-tipo; `aliases`/`domains` son la superficie de
  resolución determinista (nombre alterno / dominio de email). `interest` marca la lista curada.
- `mod_identidades_persons` — personas (sync de Google Contacts + manuales). Idempotencia de sync
  por `(provider, provider_account_id, provider_resource_name)` (índice UNIQUE parcial, calca
  `mod_calendar_events_provider_uniq`). `emails` con GIN para resolución por email.
- `mod_identidades_person_orgs` — asociación persona↔org (una persona pertenece a / se asocia con
  una organización).
- `mod_identidades_mentions` — TARGET de `IdentidadesModule.persist()`. Menciones crudas extraídas
  de mensajes por el LLM (calca el shape `source_inbox_ids` + `evidence` de `mod_finance_expenses`
  / `mod_calendar_events`): append-only, por-mensaje, nunca canónicas. La resolución determinista
  escribe `resolved_*_id` + `resolution_method` (traza auditable de cómo se ató la mención a la
  entidad canónica). El set canónico se siembra EXTERNAMENTE (Contacts + lista de interés), así que
  este slice NO necesita dedup/merge LLM (a diferencia de calendar).

SUBSTRATE-READY (NO se construye en este slice): cada persona/org tiene `id BIGSERIAL` bajo
`user_id` → endpoint estable `("identidades", id)` para una futura tabla de aristas, sin cambios
de schema. `mentions.source_inbox_ids` (GIN) es la llave de join compartida con finance/calendar →
una arista persona↔evento/gasto se materializa después uniendo `source_inbox_ids`, sin re-extraer.
NO se crean tablas de aristas, FK al substrato, ni `contribute` acá.

NUMERACIÓN (migration-numbering-worktrees): se crea como `0027_identidades` chaineando sobre 0026
(cabeza de `main` al crear el worktree). El worktree del substrato de relaciones también reclama el
0027 (`0027_relation_edges`, sin mergear): por eso este usa un revision id CON SUFIJO descriptivo
(no el `"0027"` pelado), de modo que al mergear ambas ramas queden DOS cabezas sobre 0026
(`alembic merge`) en vez de un error de id duplicado.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027_identidades"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Cuenta de proveedor + cursor de sync incremental. `account_id` → fila `accounts` del
    #    dashboard cuyo vault tiene el `google_oauth_token` (Decisión 6). Referencia al secreto,
    #    no el secreto.
    op.execute(
        """
        CREATE TABLE mod_identidades_provider_accounts (
            id              BIGSERIAL PRIMARY KEY,
            user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider        TEXT NOT NULL,
            account_label   TEXT NOT NULL,
            account_id      BIGINT REFERENCES accounts(id) ON DELETE SET NULL,
            sync_token      TEXT,
            last_sync_at    TIMESTAMPTZ,
            enabled         BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, provider, account_label)
        );
        CREATE INDEX mod_identidades_provider_accounts_user
            ON mod_identidades_provider_accounts (user_id) WHERE enabled;
        """
    )

    # 2. Organizaciones / productos / agentes (lista de interés). `aliases`/`domains` = superficie
    #    de resolución determinista. `source` incluye 'google_contacts' porque el sync puede crear
    #    una org desde el `org_name` de un contacto.
    op.execute(
        """
        CREATE TABLE mod_identidades_orgs (
            id           BIGSERIAL PRIMARY KEY,
            user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name         TEXT NOT NULL,
            kind         TEXT NOT NULL DEFAULT 'organizacion'
                           CHECK (kind IN ('organizacion','producto','agente')),
            aliases      TEXT[] NOT NULL DEFAULT '{}',
            domains      TEXT[] NOT NULL DEFAULT '{}',
            interest     BOOLEAN NOT NULL DEFAULT TRUE,
            description  TEXT NOT NULL DEFAULT '',
            source       TEXT NOT NULL DEFAULT 'manual'
                           CHECK (source IN ('manual','extraction','google_contacts')),
            metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, name)
        );
        -- Resolución case-insensitive por nombre (además del UNIQUE plano de arriba).
        CREATE UNIQUE INDEX mod_identidades_orgs_user_lname
            ON mod_identidades_orgs (user_id, lower(name));
        CREATE INDEX mod_identidades_orgs_aliases
            ON mod_identidades_orgs USING GIN (aliases);
        CREATE INDEX mod_identidades_orgs_domains
            ON mod_identidades_orgs USING GIN (domains);
        CREATE INDEX mod_identidades_orgs_user_interest
            ON mod_identidades_orgs (user_id, interest);
        """
    )

    # 3. Personas (sync de Google Contacts + manuales). Idempotencia de sync por el id estable de la
    #    People API (`provider_resource_name`, p.ej. `people/c123…`); `emails` con GIN para resolver
    #    menciones por email.
    op.execute(
        """
        CREATE TABLE mod_identidades_persons (
            id                     BIGSERIAL PRIMARY KEY,
            user_id                BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            display_name           TEXT NOT NULL,
            given_name             TEXT,
            family_name            TEXT,
            emails                 TEXT[] NOT NULL DEFAULT '{}',
            phones                 TEXT[] NOT NULL DEFAULT '{}',
            handles                JSONB NOT NULL DEFAULT '{}'::jsonb,
            org_name               TEXT,
            role                   TEXT,
            source                 TEXT NOT NULL DEFAULT 'manual'
                                     CHECK (source IN ('google_contacts','manual','extraction')),
            interest               BOOLEAN NOT NULL DEFAULT FALSE,
            provider               TEXT,
            provider_account_id    BIGINT
                                     REFERENCES mod_identidades_provider_accounts(id)
                                     ON DELETE SET NULL,
            provider_resource_name TEXT,
            provider_etag          TEXT,
            photo_url              TEXT,
            notes                  TEXT NOT NULL DEFAULT '',
            metadata               JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        -- Idempotencia: un contacto del proveedor no se duplica entre corridas de sync.
        CREATE UNIQUE INDEX mod_identidades_persons_provider_uniq
            ON mod_identidades_persons (provider, provider_account_id, provider_resource_name)
            WHERE provider_resource_name IS NOT NULL;
        CREATE INDEX mod_identidades_persons_user_name
            ON mod_identidades_persons (user_id, display_name);
        CREATE INDEX mod_identidades_persons_emails
            ON mod_identidades_persons USING GIN (emails);
        CREATE INDEX mod_identidades_persons_interest
            ON mod_identidades_persons (user_id, interest);
        """
    )

    # 4. Asociación persona↔org.
    op.execute(
        """
        CREATE TABLE mod_identidades_person_orgs (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            person_id   BIGINT NOT NULL
                          REFERENCES mod_identidades_persons(id) ON DELETE CASCADE,
            org_id      BIGINT NOT NULL
                          REFERENCES mod_identidades_orgs(id) ON DELETE CASCADE,
            role        TEXT,
            source      TEXT NOT NULL DEFAULT 'manual'
                          CHECK (source IN ('google_contacts','manual','extraction')),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (person_id, org_id)
        );
        CREATE INDEX mod_identidades_person_orgs_org
            ON mod_identidades_person_orgs (org_id);
        """
    )

    # 5. Menciones crudas (target de persist()). `source_inbox_ids` = atribución por-mensaje (sin FK;
    #    GIN). `resolved_*` + `resolution_method` = atado determinista a la entidad canónica.
    op.execute(
        """
        CREATE TABLE mod_identidades_mentions (
            id                 BIGSERIAL PRIMARY KEY,
            user_id            BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_inbox_ids   BIGINT[] NOT NULL,
            evidence           TEXT NOT NULL DEFAULT '',
            mentioned_name     TEXT NOT NULL,
            mentioned_kind     TEXT NOT NULL DEFAULT 'unknown'
                                 CHECK (mentioned_kind IN
                                   ('persona','organizacion','producto','agente','unknown')),
            email              TEXT,
            handle             TEXT,
            org_hint           TEXT,
            role_hint          TEXT,
            confidence         NUMERIC(4,3),
            resolved_kind      TEXT CHECK (resolved_kind IN ('person','org')),
            resolved_person_id BIGINT
                                 REFERENCES mod_identidades_persons(id) ON DELETE SET NULL,
            resolved_org_id    BIGINT
                                 REFERENCES mod_identidades_orgs(id) ON DELETE SET NULL,
            resolution_method  TEXT CHECK (resolution_method IN
                                   ('email','handle','exact_name','alias','domain','created',
                                    'unresolved')),
            metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX mod_identidades_mentions_inbox_ids
            ON mod_identidades_mentions USING GIN (source_inbox_ids);
        CREATE INDEX mod_identidades_mentions_person
            ON mod_identidades_mentions (user_id, resolved_person_id);
        CREATE INDEX mod_identidades_mentions_org
            ON mod_identidades_mentions (user_id, resolved_org_id);
        CREATE INDEX mod_identidades_mentions_user_created
            ON mod_identidades_mentions (user_id, created_at DESC);
        """
    )

    # 6. Observabilidad del sync (created/modified/deleted/unchanged por corrida).
    op.execute(
        """
        CREATE TABLE mod_identidades_sync_runs (
            id                  BIGSERIAL PRIMARY KEY,
            user_id             BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider_account_id BIGINT
                                  REFERENCES mod_identidades_provider_accounts(id)
                                  ON DELETE SET NULL,
            pulled              INTEGER NOT NULL DEFAULT 0,
            created             INTEGER NOT NULL DEFAULT 0,
            modified            INTEGER NOT NULL DEFAULT 0,
            deleted             INTEGER NOT NULL DEFAULT 0,
            unchanged           INTEGER NOT NULL DEFAULT 0,
            errors              INTEGER NOT NULL DEFAULT 0,
            status              TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','error')),
            detail              JSONB NOT NULL DEFAULT '{}'::jsonb,
            started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at         TIMESTAMPTZ
        );
        CREATE INDEX mod_identidades_sync_runs_user_started
            ON mod_identidades_sync_runs (user_id, started_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mod_identidades_sync_runs CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_identidades_mentions CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_identidades_person_orgs CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_identidades_persons CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_identidades_orgs CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_identidades_provider_accounts CASCADE;")
