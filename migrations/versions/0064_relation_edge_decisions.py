"""relation_edge_decisions + relation_edge_sources: historial de veredictos y procedencia de pistas

Revision ID: 0064_relation_edge_decisions
Revises: 0063_finance_place
Create Date: 2026-06-11

Soporte del subsistema `resolve` (veredicto par-por-par del long-tail de co-ocurrencias).
Semántica BI-CAPA: el veredicto ES la transición de status del mismo edge (`resolve_edge`,
monótono, los lectores existentes no cambian); estas tablas son el HISTORIAL — la señal original
nunca se destruye (patrón NELL candidate→promoted / Wikidata statement+references).

- `relation_edge_decisions`: log append-only de veredictos. Cada fila = quién decidió qué sobre la
  arista y con qué fundamento: `method` (regla determinista / llm / partidor de cúmulos / humano),
  `rule` (p.ej. 'recibo', 'redundante', 'cluster:{id}'), `inbox_id` (el mensaje que fundamenta;
  FK-less a propósito, molde `source_inbox_ids`), `quote` (cita grounded del LLM) y `evidence_sig`
  (sha256 del set de mensajes-evidencia del par AL DECIDIR → detección de staleness). `verdict =
  'dejar'` NO transiciona el edge: su fila es el memo "no decidible con ESTA evidencia" (no se
  re-gasta LLM mientras la sig no cambie). Sin UNIQUE: la idempotencia de `confirm`/`reject` la da
  la monotonía de `resolve_edge` (se inserta solo cuando la transición ocurrió); la de `dejar` la
  controla el resolver comparando la sig de la última decisión.
- `relation_edge_sources`: TODOS los mensajes que generaron una pista de co-ocurrencia, no solo el
  primero (`evidence='inbox:N'` conserva el primero por idempotencia de `propose_edge`; un segundo
  mensaje con el mismo par debe quedar ligado igual, requisito del dueño). Append-only; la puebla
  `_materialize_cooccurrence` en cada `build` (backfill natural, sin data migration), incluso si la
  arista ya es terminal — así un terminal que gana evidencia nueva es detectable y reportable.

`user_id` FK a users ON DELETE CASCADE en decisions (multi-tenant + el TRUNCATE de tests arrastra);
sources cae en cascada vía `relation_edges`.

Numeración (migration-numbering-worktrees): 0064 verificado libre; head lineal = 0063_finance_place.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0064_relation_edge_decisions"
down_revision: str | None = "0063_finance_place"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE relation_edge_decisions (
            id           BIGSERIAL PRIMARY KEY,
            user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            edge_id      BIGINT NOT NULL REFERENCES relation_edges(id) ON DELETE CASCADE,
            verdict      TEXT NOT NULL CHECK (verdict IN ('confirm','reject','dejar')),
            method       TEXT NOT NULL CHECK (method IN ('regla','llm','partidor','humano')),
            rule         TEXT NOT NULL DEFAULT '',
            inbox_id     BIGINT,
            quote        TEXT NOT NULL DEFAULT '',
            confidence   NUMERIC(4,3),
            evidence_sig CHAR(64) NOT NULL,
            run_id       TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX relation_edge_decisions_edge ON relation_edge_decisions (edge_id, id DESC);
        CREATE INDEX relation_edge_decisions_user ON relation_edge_decisions (user_id);

        CREATE TABLE relation_edge_sources (
            edge_id    BIGINT NOT NULL REFERENCES relation_edges(id) ON DELETE CASCADE,
            inbox_id   BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (edge_id, inbox_id)
        );
        CREATE INDEX relation_edge_sources_inbox ON relation_edge_sources (inbox_id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS relation_edge_sources CASCADE;
        DROP TABLE IF EXISTS relation_edge_decisions CASCADE;
        """
    )
