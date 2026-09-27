"""identidades v2: identidad unificada (persona|organizacion) + identificadores por-fuente + dedup difuso

Revision ID: 0033_identidades_v2
Revises: 0032
Create Date: 2026-06-04

Rediseño del módulo `identidades` (reemplaza el slice 0029). Unifica `mod_identidades_persons` y
`mod_identidades_orgs` en UNA tabla `mod_identidades` con discriminador `kind ∈ {persona,
organizacion}` (los viejos `producto`/`agente` se pliegan a `organizacion`; el original queda en
`metadata.legacy_kind`). Cambios clave frente a 0029:

- IDENTIFICADORES POR-FUENTE: `mod_identidades_identifiers` unifica email/teléfono/handle/dominio/url
  con su PLATAFORMA y procedencia. El handle de X ≠ Instagram ≠ Facebook → el match se acota por
  `(platform, kind, value_norm)`. Reemplaza `persons.emails[]/phones[]/handles` y `orgs.domains[]`.
- PERSONAS: `given_name`, `family_name`, `birthday DATE`, `notes` (campo único editable).
- ORGS: `mod_identidades_sites` (1..N: label, address, country — SIN coordenadas).
- DEDUP DIFUSO: extensiones `unaccent` + `pg_trgm` + `fuzzystrmatch`. `name_norm` (unaccent+lower+
  colapso ws) y `org_core` (name_norm + strip de sufijos legales: SAS/S.A./Ltda/Inc/Corp/LLC/...)
  son columnas GENERADAS STORED indexadas con `gin_trgm_ops`. `unaccent` (1-arg) es STABLE, así que
  NO se puede usar en columnas generadas/índices → se envuelve en `memex_immutable_unaccent`
  (truco estándar: la forma 2-arg `unaccent('unaccent', $1)` marcada IMMUTABLE).
- MENCIONES: `mod_identidades_mentions` ahora usa UN `resolved_identity_id` (no person/org
  separados); `resolution_method` extendido con 'fuzzy','llm','sender_email'.
- MERGE: `mod_identidades_merge_candidates` (cola de la zona gris + auditoría del desempate LLM,
  espeja `mod_calendar_dedup_candidates`).

`memex_norm`/`memex_org_core` (SQL) son el ESPEJO de `memex.modules.identidades.normalize`
(`normalize_match`/`org_core`): la DB normaliza para los índices/trigram; Python replica solo para
el match EXACTO en memoria (paridad verificada en tests/identidades/test_normalize.py).

Se conservan `mod_identidades_provider_accounts` y `mod_identidades_sync_runs` (0029, pueden tener
una cuenta configurada). REEMPLAZO DESTRUCTIVO de persons/orgs/person_orgs/mentions: el módulo
estaba apagado (extracción nunca habilitada, sync nunca configurado) → vacías. Hay una GUARDA que
falla ruidoso si tuvieran filas (no pierde datos en silencio). `downgrade` recrea el schema 0029
vacío (la unificación no reconstruye datos).

Numeración (migration-numbering-worktrees): 0033 verificado libre en los 3 worktrees y todas las
ramas (main y relaciones-v2-contrato llegan a 0032). `down_revision='0032'`; revision id con sufijo
descriptivo para no chocar con futuros worktrees.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0033_identidades_v2"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Sufijos legales/societarios a quitar del núcleo de orgs (Colombia/LatAm/España + anglo + euro).
#: ESPEJO de `memex.modules.identidades.normalize._ORG_SUFFIXES` — mantener en sync (test de paridad).
#: Whole-word (\m..\M), aplicados tras quitar puntos y normalizar. Ordenados largo→corto.
_ORG_SUFFIXES = (
    "incorporated",
    "corporation",
    "technologies",
    "holdings",
    "company",
    "limited",
    "holding",
    "ltda",
    "grupo",
    "group",
    "gmbh",
    "corp",
    "oyj",
    "sapi",
    "eirl",
    "inc",
    "llc",
    "llp",
    "plc",
    "ltd",
    "sas",
    "sac",
    "sca",
    "scs",
    "spa",
    "slu",
    "srl",
    "pty",
    "pte",
    "ohg",
    "co",
    "sa",
    "sl",
    "ag",
    "bv",
    "oy",
    "kk",
    "kg",
)
_ORG_SUFFIX_RE = "|".join(_ORG_SUFFIXES)


def upgrade() -> None:
    # 1. Extensiones de matching difuso. unaccent: plegar acentos (español); pg_trgm: similitud por
    #    trigramas (índices GIN); fuzzystrmatch: levenshtein (desempate determinista).
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent;")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    op.execute("CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;")

    # 2. Funciones IMMUTABLE de normalización (necesarias para columnas generadas + índices).
    #    `unaccent(text)` 1-arg es STABLE → se envuelve en la forma 2-arg marcada IMMUTABLE.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION memex_immutable_unaccent(text)
        RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS
        $$ SELECT unaccent('unaccent', $1) $$;
        """
    )
    # memex_norm: unaccent + lower + colapso de whitespace. Espejo de normalize_match (Python).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION memex_norm(text)
        RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS
        $$ SELECT lower(btrim(regexp_replace(memex_immutable_unaccent($1), '\\s+', ' ', 'g'))) $$;
        """
    )
    # memex_org_core: memex_norm + quitar puntos + puntuación→espacio + strip de sufijos legales.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION memex_org_core(text)
        RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS
        $$
          SELECT btrim(regexp_replace(
            regexp_replace(
              regexp_replace(replace(memex_norm($1), '.', ''), '[^a-z0-9]+', ' ', 'g'),
              '\\m({_ORG_SUFFIX_RE})\\M', '', 'g'),
            '\\s+', ' ', 'g'))
        $$;
        """
    )

    # 3. Reemplazo destructivo de las tablas 0029. El módulo estaba apagado (extracción nunca
    #    habilitada, sync nunca configurado) → persons/orgs/person_orgs/mentions están vacías. Se
    #    GUARDA contra datos inesperados (falla ruidoso, NO pierde datos en silencio); si alguna vez
    #    dispara, se implementa el backfill mapeado antes de re-aplicar. provider_accounts/sync_runs
    #    se conservan (pueden tener una cuenta configurada; mod_identidades las referencia).
    op.execute(
        """
        DO $$
        BEGIN
          IF (SELECT count(*) FROM mod_identidades_persons) > 0
             OR (SELECT count(*) FROM mod_identidades_orgs) > 0
             OR (SELECT count(*) FROM mod_identidades_person_orgs) > 0
             OR (SELECT count(*) FROM mod_identidades_mentions) > 0 THEN
            RAISE EXCEPTION 'mod_identidades_* (0029) no esta vacio; el backfill al modelo unificado v2 no esta implementado. Migrar los datos manualmente antes de aplicar 0033.';
          END IF;
        END $$;
        """
    )
    op.execute("DROP TABLE IF EXISTS mod_identidades_mentions CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_identidades_person_orgs CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_identidades_persons CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_identidades_orgs CASCADE;")

    # 4. Tabla base unificada. provider_account_id → mod_identidades_provider_accounts (intacta).
    op.execute(
        """
        CREATE TABLE mod_identidades (
            id                     BIGSERIAL PRIMARY KEY,
            user_id                BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind                   TEXT NOT NULL CHECK (kind IN ('persona','organizacion')),
            display_name           TEXT NOT NULL,
            aliases                TEXT[] NOT NULL DEFAULT '{}',
            interest               BOOLEAN NOT NULL DEFAULT FALSE,
            source                 TEXT NOT NULL DEFAULT 'manual'
                                     CHECK (source IN ('manual','extraction','google_contacts')),
            notes                  TEXT NOT NULL DEFAULT '',
            -- persona (NULLABLE; solo kind='persona'):
            given_name             TEXT,
            family_name            TEXT,
            birthday               DATE,
            -- sync Google Contacts:
            provider               TEXT,
            provider_account_id    BIGINT
                                     REFERENCES mod_identidades_provider_accounts(id)
                                     ON DELETE SET NULL,
            provider_resource_name TEXT,
            provider_etag          TEXT,
            photo_url              TEXT,
            metadata               JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            -- columnas generadas para match/trigram (la DB normaliza, no Python):
            name_norm TEXT GENERATED ALWAYS AS (memex_norm(display_name)) STORED,
            org_core  TEXT GENERATED ALWAYS AS (memex_org_core(display_name)) STORED
        );
        -- idempotencia del sync (un contacto del proveedor no se duplica entre corridas):
        CREATE UNIQUE INDEX mod_identidades_provider_uniq
            ON mod_identidades (provider, provider_account_id, provider_resource_name)
            WHERE provider_resource_name IS NOT NULL;
        CREATE INDEX mod_identidades_user_kind ON mod_identidades (user_id, kind);
        CREATE INDEX mod_identidades_user_interest ON mod_identidades (user_id, interest);
        -- match exacto normalizado (senal fuerte) + trigram (difuso):
        CREATE INDEX mod_identidades_name_norm ON mod_identidades (user_id, name_norm);
        CREATE INDEX mod_identidades_name_trgm ON mod_identidades USING GIN (name_norm gin_trgm_ops);
        CREATE INDEX mod_identidades_orgcore_trgm ON mod_identidades USING GIN (org_core gin_trgm_ops)
            WHERE kind = 'organizacion';
        -- alias exacto (igualdad contra el array aliases):
        CREATE INDEX mod_identidades_aliases ON mod_identidades USING GIN (aliases);
        """
    )

    # 5. Identificadores por-fuente (unifica email/phone/handle/domain/url). Match acotado por
    #    plataforma. value_norm lo computa Python (norm_identifier) según el kind.
    op.execute(
        """
        CREATE TABLE mod_identidades_identifiers (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            identity_id BIGINT NOT NULL REFERENCES mod_identidades(id) ON DELETE CASCADE,
            platform    TEXT NOT NULL,
            kind        TEXT NOT NULL CHECK (kind IN ('email','phone','handle','domain','url')),
            value       TEXT NOT NULL,
            value_norm  TEXT NOT NULL,
            is_primary  BOOLEAN NOT NULL DEFAULT FALSE,
            source      TEXT NOT NULL DEFAULT 'manual',
            metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (identity_id, platform, kind, value_norm)
        );
        CREATE INDEX mod_identidades_idf_match
            ON mod_identidades_identifiers (user_id, platform, kind, value_norm);
        CREATE INDEX mod_identidades_idf_identity
            ON mod_identidades_identifiers (identity_id);
        """
    )

    # 6. Sedes (1..N, solo orgs). Sin coordenadas.
    op.execute(
        """
        CREATE TABLE mod_identidades_sites (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            identity_id BIGINT NOT NULL REFERENCES mod_identidades(id) ON DELETE CASCADE,
            label       TEXT NOT NULL DEFAULT '',
            address     TEXT NOT NULL DEFAULT '',
            country     TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX mod_identidades_sites_identity ON mod_identidades_sites (identity_id);
        """
    )

    # 7. Afiliación persona↔org (ambos FK a mod_identidades).
    op.execute(
        """
        CREATE TABLE mod_identidades_person_orgs (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            person_id   BIGINT NOT NULL REFERENCES mod_identidades(id) ON DELETE CASCADE,
            org_id      BIGINT NOT NULL REFERENCES mod_identidades(id) ON DELETE CASCADE,
            role        TEXT,
            source      TEXT NOT NULL DEFAULT 'manual',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (person_id, org_id)
        );
        CREATE INDEX mod_identidades_person_orgs_org ON mod_identidades_person_orgs (org_id);
        """
    )

    # 8. Menciones (evidencia por-mensaje). UN resolved_identity_id; resolution_method extendido.
    op.execute(
        """
        CREATE TABLE mod_identidades_mentions (
            id                   BIGSERIAL PRIMARY KEY,
            user_id              BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_inbox_ids     BIGINT[] NOT NULL,
            evidence             TEXT NOT NULL DEFAULT '',
            mentioned_name       TEXT NOT NULL,
            mentioned_kind       TEXT NOT NULL DEFAULT 'unknown'
                                   CHECK (mentioned_kind IN
                                     ('persona','organizacion','producto','agente','unknown')),
            email                TEXT,
            handle               TEXT,
            org_hint             TEXT,
            role_hint            TEXT,
            confidence           NUMERIC(4,3),
            resolved_kind        TEXT CHECK (resolved_kind IN ('persona','organizacion')),
            resolved_identity_id BIGINT REFERENCES mod_identidades(id) ON DELETE SET NULL,
            resolution_method    TEXT CHECK (resolution_method IN
                                   ('email','handle','exact_name','alias','domain','created',
                                    'unresolved','fuzzy','llm','sender_email')),
            metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX mod_identidades_mentions_inbox_ids
            ON mod_identidades_mentions USING GIN (source_inbox_ids);
        CREATE INDEX mod_identidades_mentions_identity
            ON mod_identidades_mentions (user_id, resolved_identity_id);
        CREATE INDEX mod_identidades_mentions_user_created
            ON mod_identidades_mentions (user_id, created_at DESC);
        """
    )

    # 9. Candidatos de merge (zona gris + auditoría del desempate LLM). Espeja
    #    mod_calendar_dedup_candidates. Par canónico (a < b) para idempotencia.
    op.execute(
        """
        CREATE TABLE mod_identidades_merge_candidates (
            id            BIGSERIAL PRIMARY KEY,
            user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            identity_a_id BIGINT NOT NULL REFERENCES mod_identidades(id) ON DELETE CASCADE,
            identity_b_id BIGINT NOT NULL REFERENCES mod_identidades(id) ON DELETE CASCADE,
            reason        TEXT NOT NULL,
            score         NUMERIC(4,3),
            status        TEXT NOT NULL DEFAULT 'candidate'
                            CHECK (status IN ('candidate','confirmed','rejected')),
            decided_by    TEXT,
            confidence    NUMERIC(4,3),
            rationale     TEXT,
            decided_at    TIMESTAMPTZ,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT mod_identidades_merge_candidates_order CHECK (identity_a_id < identity_b_id),
            CONSTRAINT mod_identidades_merge_candidates_uq UNIQUE (identity_a_id, identity_b_id)
        );
        CREATE INDEX mod_identidades_merge_user_status
            ON mod_identidades_merge_candidates (user_id, status);
        """
    )


def downgrade() -> None:
    # Recrea el schema 0029 (VACÍO). La unificación es destructiva: no reconstruye datos. Las
    # extensiones y las funciones memex_* quedan (no las usa nada más, dropearlas no aporta).
    op.execute("DROP TABLE IF EXISTS mod_identidades_merge_candidates CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_identidades_mentions CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_identidades_person_orgs CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_identidades_sites CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_identidades_identifiers CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_identidades CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS memex_org_core(text);")
    op.execute("DROP FUNCTION IF EXISTS memex_norm(text);")
    op.execute("DROP FUNCTION IF EXISTS memex_immutable_unaccent(text);")

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
        CREATE UNIQUE INDEX mod_identidades_orgs_user_lname
            ON mod_identidades_orgs (user_id, lower(name));
        CREATE INDEX mod_identidades_orgs_aliases ON mod_identidades_orgs USING GIN (aliases);
        CREATE INDEX mod_identidades_orgs_domains ON mod_identidades_orgs USING GIN (domains);
        CREATE INDEX mod_identidades_orgs_user_interest
            ON mod_identidades_orgs (user_id, interest);
        """
    )
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
