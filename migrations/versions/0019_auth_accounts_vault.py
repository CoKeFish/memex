"""auth + accounts + vault de credenciales cifradas

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-01

Introduce el login multi-usuario y el vault de credenciales de ingestor. Reemplaza el patrón
env-var-by-name (que se mantiene como FALLBACK durante la transición) por credenciales CIFRADAS
en la DB.

Modelo (ver memoria credentials-vault-architecture):
- `user_credentials` (1:1 con users): hash Argon2id de la contraseña (solo login) + DEK por-usuario
  envuelto con la MASTER KEY del servidor (MEMEX_SECRET_KEY, Doppler; global, una sola vez). El DEK
  en claro NUNCA se guarda; la contraseña NO deriva ninguna llave de cifrado (por eso el reset de
  contraseña no pierde credenciales).
- `sessions`: login del dashboard (cookie httpOnly). `id` = sha256 hex del token opaco — el token
  plano nunca toca la DB. La sesión es SOLO auth; no guarda ninguna llave.
- `accounts`: cuenta de primera clase que agrupa sources; `alias` lo define el usuario.
- `account_secrets`: secretos por cuenta CIFRADOS (AES-256-GCM bajo el DEK del usuario). `last4` es
  no-secreto (máscara UI). El plaintext nunca está en la DB ni en logs.
- `sources.account_id`: vincula una source a su cuenta (nullable → sources existentes intactas).

ENMIENDA DE ADR-001: a diferencia de `mod_calendar_provider_accounts.token_path_env` (que guarda el
NOMBRE de una env var), acá el secreto vive CIFRADO en la DB. Sigue cumpliendo el aislamiento del
ingestor: el descifrado ocurre fuera de `memex.ingestors` (en `memex.security` + `sources/resolver`),
que inyecta el plaintext en el `env` resuelto bajo el mismo nombre de env var que el `cfg` referencia.

Todo el DDL es ADITIVO (tablas nuevas + columna nullable) → no rompe lo existente. El back-fill crea
una `accounts` por cada source ya existente (sin secretos: esas sources siguen resolviendo por el
fallback env hasta que el usuario migre la credencial por la UI).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Auth + vault por-usuario (1:1 con users). El DEK envuelto con la master key del servidor.
    op.execute(
        """
        CREATE TABLE user_credentials (
            user_id       BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            password_hash TEXT NOT NULL,
            wrapped_dek   BYTEA NOT NULL,
            dek_nonce     BYTEA NOT NULL,
            key_version   SMALLINT NOT NULL DEFAULT 1,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    # 2. Sesiones del dashboard. id = sha256(token opaco); el token plano nunca se persiste.
    op.execute(
        """
        CREATE TABLE sessions (
            id           TEXT PRIMARY KEY,
            user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at   TIMESTAMPTZ NOT NULL,
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            revoked_at   TIMESTAMPTZ,
            user_agent   TEXT,
            client_ip    TEXT
        );
        CREATE INDEX sessions_user_active ON sessions (user_id) WHERE revoked_at IS NULL;
        CREATE INDEX sessions_expires ON sessions (expires_at);
        """
    )

    # 3. Cuentas: agrupan sources; el alias lo pone el usuario. metadata = no-secretos (server/port,
    # oauth_provider, ...). health_* lo actualiza el endpoint de health-check.
    op.execute(
        """
        CREATE TABLE accounts (
            id                   BIGSERIAL PRIMARY KEY,
            user_id              BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            alias                TEXT NOT NULL,
            provider             TEXT NOT NULL,
            kind                 TEXT NOT NULL CHECK (kind IN ('email','chat','social')),
            metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
            enabled              BOOLEAN NOT NULL DEFAULT TRUE,
            health_status        TEXT NOT NULL DEFAULT 'unknown'
                                   CHECK (health_status IN
                                     ('unknown','healthy','degraded','unhealthy')),
            last_health_check_at TIMESTAMPTZ,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, alias)
        );
        CREATE INDEX accounts_user ON accounts (user_id);
        """
    )

    # 4. Secretos por cuenta, CIFRADOS bajo el DEK del usuario (AES-256-GCM). last4 = no-secreto.
    op.execute(
        """
        CREATE TABLE account_secrets (
            id          BIGSERIAL PRIMARY KEY,
            account_id  BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            secret_name TEXT NOT NULL,
            ciphertext  BYTEA NOT NULL,
            nonce       BYTEA NOT NULL,
            enc_version SMALLINT NOT NULL DEFAULT 1,
            last4       TEXT NOT NULL DEFAULT '',
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (account_id, secret_name)
        );
        """
    )

    # 5. Vincular sources a cuentas (nullable → sources existentes intactas; fallback env si NULL).
    op.execute(
        """
        ALTER TABLE sources
            ADD COLUMN account_id BIGINT REFERENCES accounts(id) ON DELETE SET NULL;
        CREATE INDEX sources_account ON sources (account_id) WHERE account_id IS NOT NULL;
        """
    )

    # 6. Back-fill: una account por cada source existente (sin secretos). kind por tipo conocido;
    # tipos sin kind conocido NO se migran (siguen con account_id NULL → fallback env).
    op.execute(
        """
        INSERT INTO accounts (user_id, alias, provider, kind, enabled)
        SELECT s.user_id, s.name, s.type,
               CASE s.type
                 WHEN 'imap'      THEN 'email'
                 WHEN 'outlook'   THEN 'email'
                 WHEN 'telegram'  THEN 'chat'
                 WHEN 'instagram' THEN 'social'
                 WHEN 'facebook'  THEN 'social'
                 WHEN 'x'         THEN 'social'
               END,
               s.enabled
        FROM sources s
        WHERE s.type IN ('imap','outlook','telegram','instagram','facebook','x');

        UPDATE sources s
        SET account_id = a.id
        FROM accounts a
        WHERE a.user_id = s.user_id AND a.alias = s.name AND s.account_id IS NULL;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE sources DROP COLUMN IF EXISTS account_id;")
    op.execute("DROP TABLE IF EXISTS account_secrets CASCADE;")
    op.execute("DROP TABLE IF EXISTS accounts CASCADE;")
    op.execute("DROP TABLE IF EXISTS sessions CASCADE;")
    op.execute("DROP TABLE IF EXISTS user_credentials CASCADE;")
