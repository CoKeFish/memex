"""relation_clusters: cúmulos del grafo (detección de comunidades + validación LLM)

Revision ID: 0053_relation_clusters
Revises: 0052_relevance_llm_verdict
Create Date: 2026-06-08

Un CÚMULO es "solo una colección de vértices que aseguramos que están relacionados" (modelo del
dueño). Lo detecta Louvain sobre `relation_edges` (community detection) y lo valida un LLM que lo
confirma/nombra/describe/poda. NO es una caja sellada: emerge en el grafo único y, al confirmarse, se
materializa como vértice nativo `cumulo` (proyectado de esta tabla en `relations/vertices`) + aristas
`miembro_de` hacia sus miembros.

- `relation_clusters`: un cúmulo. `signature` = sha256 del set de miembros DETECTADO (ordenado), base
  de la idempotencia de detección y del memo de rechazo. `status`: candidate (detectado, sin validar) ·
  confirmed (el LLM lo avaló) · stale (faltó alguna corrida; vive en gracia — slice posterior) ·
  rejected (el LLM lo descartó; `signature` queda de MEMO para no re-proponerlo) · dissolved
  (desapareció del grafo). `validated_signature` = la firma con la que se validó (auditoría /
  revival). `needs_revalidation` = la membresía derivó lo bastante como para re-juzgar.
- **Unique PARCIAL** `(user_id, signature) WHERE status IN ('candidate','rejected')`: idempotencia de
  detección + memo de rechazo, SIN trabar el drift de firma de los confirmed/stale (que sí cambian de
  miembros entre corridas). Es un índice parcial (no un CONSTRAINT): los inserts usan
  `ON CONFLICT (user_id, signature) WHERE status IN (...)`, no `ON CONSTRAINT`.
- `relation_cluster_members`: la membresía. `pruned=TRUE` = el LLM lo sacó del cúmulo (no proyecta
  arista, pero SIGUE contando para la `signature`/Jaccard = el set detectado; la detección no conoce
  la poda). UNIQUE `(cluster_id, member_slug, member_id)` → idempotencia de la membresía.
- `user_id` FK a users ON DELETE CASCADE (multi-tenant + el TRUNCATE de tests arrastra ambas tablas).

Numeración (migration-numbering-worktrees): 0053 verificado libre; head lineal = 0052_relevance_llm_verdict.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0053_relation_clusters"
down_revision: str | None = "0052_relevance_llm_verdict"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE relation_clusters (
            id                  BIGSERIAL PRIMARY KEY,
            user_id             BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status              TEXT NOT NULL DEFAULT 'candidate'
                                  CHECK (status IN ('candidate','confirmed','stale','rejected','dissolved')),
            name                TEXT NOT NULL DEFAULT '',
            description         TEXT NOT NULL DEFAULT '',
            confidence          NUMERIC(4,3),
            member_count        INT NOT NULL DEFAULT 0,
            signature           CHAR(64) NOT NULL,
            validated_signature CHAR(64),
            has_confirmed_edge  BOOLEAN NOT NULL DEFAULT FALSE,
            needs_revalidation  BOOLEAN NOT NULL DEFAULT FALSE,
            miss_count          INT NOT NULL DEFAULT 0,
            algo_meta           JSONB,
            run_id              TEXT,
            first_detected_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            validated_at        TIMESTAMPTZ,
            decided_at          TIMESTAMPTZ,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX relation_clusters_user_status ON relation_clusters (user_id, status);
        CREATE UNIQUE INDEX relation_clusters_sig_uq
            ON relation_clusters (user_id, signature)
            WHERE status IN ('candidate','rejected');

        CREATE TABLE relation_cluster_members (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            cluster_id  BIGINT NOT NULL REFERENCES relation_clusters(id) ON DELETE CASCADE,
            member_slug TEXT NOT NULL,
            member_id   BIGINT NOT NULL,
            pruned      BOOLEAN NOT NULL DEFAULT FALSE,
            added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT relation_cluster_members_uq UNIQUE (cluster_id, member_slug, member_id)
        );
        CREATE INDEX relation_cluster_members_rev
            ON relation_cluster_members (user_id, member_slug, member_id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS relation_cluster_members CASCADE;
        DROP TABLE IF EXISTS relation_clusters CASCADE;
        """
    )
