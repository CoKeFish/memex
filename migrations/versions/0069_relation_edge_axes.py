"""relation_edges: vocabulario de dos ejes (procedencia por veredicto) + flags dirty (incremental)

Revision ID: 0069_relation_edge_axes
Revises: 0068_llm_consumer_settings
Create Date: 2026-06-13

Refactor del NIVEL de la arista: el enum único `status` ('pista'/'confirmed'/'rejected') se
reemplaza por DOS EJES ortogonales, para que la PROCEDENCIA viaje como parte del contrato de la
arista (DB/código/API/frontend), inspirado en el etiquetado EXTRACTED/INFERRED/AMBIGUOUS de
graphify (se copia la disciplina, no el esquema):

- `provenance` ∈ {extracted, inferred}: CÓMO lo sabemos. `extracted` = leído literal de una fuente
  determinista (un recibo, una contraparte, una afiliación) — es un hecho. `inferred` = el LLM lo
  dedujo del contexto — probablemente cierto, pero es conclusión, no textual.
- `verdict` ∈ {confirmed, rejected, ambiguous}: la DECISIÓN. `ambiguous` = sospecha sin decidir
  (antes 'pista'): o aún sin juzgar, o la IA la miró y no supo.

La etiqueta canónica visible se DERIVA de ambos (ver `edges.canonical_label`): extracted+confirmed
→ EXTRACTED, inferred+confirmed → INFERRED, inferred+rejected → INFERRED REJECTED, *+ambiguous →
AMBIGUOUS [(inferred) si la IA la miró].

Además (groundwork ADR-021, procesamiento incremental por defecto): un flag `dirty` por arista y la
tabla `relation_vertex_state` (dirty por vértice). El productor marca dirty al persistir datos
nuevos; un futuro mantenedor de cúmulos trabaja solo sobre el delta sin re-escanear todo. Acá SOLO
va el marking — el consumidor incremental (lint / cúmulos sin pasar todo el contenido al LLM) se
difiere (ADR-021: los mecanismos por capa quedan en Backlog).

`relation` (TEXT) es la justificación corta VIGENTE de la arista (el nombre de relación que el LLM
le dio, o un texto determinista). El historial completo sigue en `relation_edge_decisions`.

Backfill de `provenance`: por la última decisión (`method` llm/partidor → inferred; regla/humano →
extracted) y, sin decisión, por el `producer` (`llm` → inferred; resto → extracted). `verdict`:
1:1 con `status` (pista→ambiguous). Todo determinista, sin pérdida.

NO confundir con `relation_clusters.status` (candidate/confirmed/stale/...): ese es el ciclo de
vida del cúmulo y NO se toca.

Numeración (migration-numbering-worktrees): 0069 verificado libre en main + worktrees
(llm-providers, resolve-pistas, confirm-cooc); head lineal = 0068_llm_consumer_settings.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0069_relation_edge_axes"
down_revision: str | None = "0068_llm_consumer_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        -- 1. columnas nuevas (nullable para backfillar los dos ejes)
        ALTER TABLE relation_edges ADD COLUMN provenance TEXT;
        ALTER TABLE relation_edges ADD COLUMN verdict    TEXT;
        ALTER TABLE relation_edges ADD COLUMN relation   TEXT NOT NULL DEFAULT '';
        ALTER TABLE relation_edges ADD COLUMN dirty      BOOLEAN NOT NULL DEFAULT TRUE;

        -- 2. backfill verdict (1:1 con status)
        UPDATE relation_edges SET verdict = CASE status
            WHEN 'pista'     THEN 'ambiguous'
            WHEN 'confirmed' THEN 'confirmed'
            WHEN 'rejected'  THEN 'rejected'
        END;

        -- 3. backfill provenance: la última decisión manda; sin decisión, por producer
        UPDATE relation_edges e SET provenance = COALESCE(
            (SELECT CASE d.method
                        WHEN 'llm'      THEN 'inferred'
                        WHEN 'partidor' THEN 'inferred'
                        WHEN 'regla'    THEN 'extracted'
                        WHEN 'humano'   THEN 'extracted'
                    END
             FROM relation_edge_decisions d
             WHERE d.edge_id = e.id
             ORDER BY d.id DESC
             LIMIT 1),
            CASE WHEN e.producer = 'llm' THEN 'inferred' ELSE 'extracted' END
        );

        -- 4. cerrar el contrato de los dos ejes. DEFAULT = estado natural de nacimiento de una
        --    co-ocurrencia (extracted+ambiguous), espejo del viejo `status DEFAULT 'pista'`:
        --    `propose_edge` igual los pasa explícito; el default solo cubre inserts crudos.
        ALTER TABLE relation_edges ALTER COLUMN provenance SET NOT NULL;
        ALTER TABLE relation_edges ALTER COLUMN verdict    SET NOT NULL;
        ALTER TABLE relation_edges ALTER COLUMN provenance SET DEFAULT 'extracted';
        ALTER TABLE relation_edges ALTER COLUMN verdict    SET DEFAULT 'ambiguous';
        ALTER TABLE relation_edges ADD CONSTRAINT relation_edges_provenance_chk
            CHECK (provenance IN ('extracted','inferred'));
        ALTER TABLE relation_edges ADD CONSTRAINT relation_edges_verdict_chk
            CHECK (verdict IN ('confirmed','rejected','ambiguous'));

        -- 5. retirar status (su CHECK inline cae con la columna) + reindexar
        DROP INDEX IF EXISTS relation_edges_user_status;
        ALTER TABLE relation_edges DROP COLUMN status;
        CREATE INDEX relation_edges_user_verdict ON relation_edges (user_id, verdict);
        CREATE INDEX relation_edges_dirty ON relation_edges (user_id) WHERE dirty;

        -- 6. dirty por vértice (groundwork incremental; la pueblan build_relations + la fase
        --    de confirmación, NO los módulos de dominio). Keyed por (slug,id) como las aristas.
        CREATE TABLE relation_vertex_state (
            user_id  BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            slug     TEXT NOT NULL,
            id       BIGINT NOT NULL,
            dirty    BOOLEAN NOT NULL DEFAULT TRUE,
            dirty_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, slug, id)
        );
        CREATE INDEX relation_vertex_state_dirty ON relation_vertex_state (user_id) WHERE dirty;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS relation_vertex_state CASCADE;

        ALTER TABLE relation_edges ADD COLUMN status TEXT NOT NULL DEFAULT 'pista'
            CHECK (status IN ('pista','confirmed','rejected'));
        UPDATE relation_edges SET status = CASE verdict
            WHEN 'ambiguous' THEN 'pista'
            WHEN 'confirmed' THEN 'confirmed'
            WHEN 'rejected'  THEN 'rejected'
        END;

        DROP INDEX IF EXISTS relation_edges_dirty;
        DROP INDEX IF EXISTS relation_edges_user_verdict;
        CREATE INDEX relation_edges_user_status ON relation_edges (user_id, status);

        ALTER TABLE relation_edges DROP CONSTRAINT IF EXISTS relation_edges_verdict_chk;
        ALTER TABLE relation_edges DROP CONSTRAINT IF EXISTS relation_edges_provenance_chk;
        ALTER TABLE relation_edges DROP COLUMN IF EXISTS dirty;
        ALTER TABLE relation_edges DROP COLUMN IF EXISTS relation;
        ALTER TABLE relation_edges DROP COLUMN IF EXISTS verdict;
        ALTER TABLE relation_edges DROP COLUMN IF EXISTS provenance;
        """
    )
