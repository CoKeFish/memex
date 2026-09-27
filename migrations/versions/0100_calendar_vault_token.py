"""Cuenta de calendario ligada a la cuenta del dashboard: el token OAuth sale del vault

Revision ID: 0100_calendar_vault_token
Revises: 0099_calendar_push_policy
Create Date: 2026-09-11

El módulo de calendario leía el token OAuth de Google de un ARCHIVO en disco (`token_path_env`),
autorizado aparte por consola, mientras el dashboard ya guarda ese mismo token cifrado en el vault
de la cuenta al «Conectar con Google» (decisión 6: un solo consentimiento con todos los scopes, el de
Calendar incluido). Identidades ya lo reusa así (`mod_identidades_provider_accounts.account_id`).

`account_id` → `accounts(id)`: si está, el token se descifra del vault de esa cuenta y el archivo no
se usa. `token_path_env` queda como vía legacy (VPS sin dashboard) y deja de ser obligatorio.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0100_calendar_vault_token"
down_revision: str | None = "0099_calendar_push_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_calendar_provider_accounts
            ADD COLUMN account_id BIGINT REFERENCES accounts(id) ON DELETE SET NULL,
            ALTER COLUMN token_path_env DROP NOT NULL;
        CREATE INDEX mod_calendar_provider_accounts_account
            ON mod_calendar_provider_accounts (account_id) WHERE account_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS mod_calendar_provider_accounts_account;
        UPDATE mod_calendar_provider_accounts
            SET token_path_env = 'GOOGLE_CALENDAR_TOKEN_PATH' WHERE token_path_env IS NULL;
        ALTER TABLE mod_calendar_provider_accounts
            ALTER COLUMN token_path_env SET NOT NULL,
            DROP COLUMN IF EXISTS account_id;
        """
    )
