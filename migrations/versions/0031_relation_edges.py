"""relation_edges: capa de aristas del grafo (vértices únicos + procedencia)

Revision ID: 0031
Revises: 0030
Create Date: 2026-06-03

UNA tabla de aristas tipadas entre VÉRTICES del grafo (filas `mod_*` o vértices nativos del grafo).
Guarda REFERENCIAS `(slug, id)`, no datos (ADR-015: cada módulo sigue dueño de sus `mod_*`; no hay
tabla central de hechos). Adapta la tabla del substrato v1 (rama no mergeada) al modelo v2 acordado:

- CUALQUIER vértice puede conectarse con cualquiera: NO hay ontología que restrinja pares legales.
  `relation_type` es una ETIQUETA descriptiva LIBRE (default ''), no un vocabulario cerrado.
- Lo OBLIGATORIO de cada arista es su `producer`: quién/qué la formó — `inbox`, `dedup`,
  `consolidacion`, `identidades`, `llm`, `humano`, ... Vocabulario ABIERTO (sin CHECK enum): se
  extiende sin migración (la typo-safety vive en Python, como `capabilities`/`CAP_*`).
- `status` NULL = HECHO determinista (inbox/dedup/consolidación): no tiene cola de revisión. Las
  INFERENCIAS (LLM cross-dominio, candidato de dedup) nacen `pending` y transicionan a
  `confirmed`/`rejected` (monótono). El índice de `status` es PARCIAL (solo la cola).
- UNIQUE lógica INCLUYE `producer`: idempotencia por productor + dos procedencias del mismo par
  (p.ej. `inbox` y `llm`) coexisten como aristas independientes.
- CHECK anti self-loop: un vértice no se enlaza consigo mismo.
- `user_id` FK a users ON DELETE CASCADE (multi-tenant + el TRUNCATE de tests arrastra la tabla).
- `seed_tag`: marca opcional para aislar/limpiar el seed cuando convive con datos reales en dev.

Numeración (migration-numbering-worktrees): 0031 verificado libre en los 3 worktrees y todas las
ramas; `down_revision='0030'`. Los cúmulos (vértices nativos del grafo) llegan en una migración
posterior, junto con el decisor LLM — NO acá (anti-especulación).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE relation_edges (
            id            BIGSERIAL PRIMARY KEY,
            user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            src_slug      TEXT NOT NULL,
            src_id        BIGINT NOT NULL,
            dst_slug      TEXT NOT NULL,
            dst_id        BIGINT NOT NULL,
            relation_type TEXT NOT NULL DEFAULT '',
            producer      TEXT NOT NULL,
            confidence    NUMERIC(4,3),
            evidence      TEXT NOT NULL DEFAULT '',
            status        TEXT CHECK (status IS NULL OR status IN ('pending','confirmed','rejected')),
            decided_at    TIMESTAMPTZ,
            seed_tag      TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT relation_edges_no_selfloop
                CHECK (NOT (src_slug = dst_slug AND src_id = dst_id)),
            CONSTRAINT relation_edges_logical_uq
                UNIQUE (user_id, src_slug, src_id, dst_slug, dst_id, relation_type, producer)
        );
        CREATE INDEX relation_edges_src ON relation_edges (user_id, src_slug, src_id);
        CREATE INDEX relation_edges_dst ON relation_edges (user_id, dst_slug, dst_id);
        CREATE INDEX relation_edges_user_status ON relation_edges (user_id, status)
            WHERE status IS NOT NULL;
        CREATE INDEX relation_edges_seed_tag ON relation_edges (seed_tag) WHERE seed_tag IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS relation_edges CASCADE;")
