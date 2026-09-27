"""Selección de proveedor+modelo LLM por consumidor (`llm_consumer_settings`)

Revision ID: 0068_llm_consumer_settings
Revises: 0067_gate_provider
Create Date: 2026-06-12

Punto único de configuración para la fábrica `memex.llm.registry.build_llm_client`: una fila por
(user_id, consumer) decide qué proveedor + modelo usa cada proceso que consume LLM cuando el
caller no inyecta un cliente. Patrón `relevance_gate_settings` (0065): la DB manda en runtime;
SIN fila para el consumer se resuelve la fila `consumer='default'`; sin esa, la fábrica cae al
hardcode DeepSeek (preserva el comportamiento previo a esta tabla, cuando cada worker construía
`DeepSeekClient(LLMConfig.from_env())`).

- `provider`: proveedor primario ('deepseek' default, 'anthropic' o 'codex'). El CHECK en la DB
  es el guardarraíl; la validación accionable vive en `upsert_consumer_settings`.
- `model`: modelo del proveedor primario (NULL = su default_model). Codex lo ignora.
- `codex_model`: modelo de codex cuando el proveedor (primario o de fallback) es codex.
- `fallback`: lista ORDENADA de proveedores extra que prueba el `FallbackClient` si el primario
  agota cuota/red-5xx/timeout (cadena efectiva = [provider, *fallback]). `[]` = sin fallback.

Numeración (migration-numbering-worktrees): 0068 verificado libre en main + worktrees
relevancia-gate/resolve-pistas; head lineal = 0067_gate_provider.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0068_llm_consumer_settings"
down_revision: str | None = "0067_gate_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE llm_consumer_settings (
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            consumer    TEXT NOT NULL CHECK (length(btrim(consumer)) > 0),
            provider    TEXT NOT NULL DEFAULT 'deepseek'
                          CHECK (provider IN ('deepseek','anthropic','codex')),
            model       TEXT,
            codex_model TEXT,
            fallback    JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, consumer)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS llm_consumer_settings CASCADE;")
