"""Provider 'openai' en llm_consumer_settings (API directa de OpenAI, además del CLI de codex)

Revision ID: 0079_openai_provider
Revises: 0078_identidades_resolver
Create Date: 2026-06-20

Suma 'openai' al CHECK de `llm_consumer_settings.provider`: la API DIRECTA de OpenAI (gpt-*, vía
`OPENAI_API_KEY`) como proveedor de primera clase — además de 'codex' (el CLI agéntico host-side,
~25s/llamada). El cliente vive en `memex.llm.openai` (espejo de DeepSeek, mismo dialecto).

Numeración (migration-numbering-worktrees): head lineal = 0078_identidades_resolver.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0079_openai_provider"
down_revision: str | None = "0078_identidades_resolver"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE llm_consumer_settings DROP CONSTRAINT llm_consumer_settings_provider_check;
        ALTER TABLE llm_consumer_settings ADD CONSTRAINT llm_consumer_settings_provider_check
            CHECK (provider IN ('deepseek','anthropic','codex','openai'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE llm_consumer_settings DROP CONSTRAINT llm_consumer_settings_provider_check;
        ALTER TABLE llm_consumer_settings ADD CONSTRAINT llm_consumer_settings_provider_check
            CHECK (provider IN ('deepseek','anthropic','codex'));
        """
    )
