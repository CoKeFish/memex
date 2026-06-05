"""finance v2: mod_finance_transactions + dedup 2 fases + consolidación

Revision ID: 0034
Revises: 0032
Create Date: 2026-06-04

El módulo finance pasa de extractor PLANO solo-gastos (`mod_finance_expenses`, un `merchant`, fecha
opcional, dedup por business-key del índice 0030) a un módulo de TRANSACCIONES completo que calca el
patrón de calendar (cru­das → dedup FASE 1 procedimental → FASE 2 LLM → consolidación):

- `mod_finance_transactions` — transacción cruda. Agrega `direction` (ingreso/egreso, obligatorio),
  parte el viejo `merchant` en `counterparty` (quién cobró/pagó — seam de identidad, TEXT por ahora) +
  `place` (lugar/URL), y reemplaza `occurred_on DATE` por `occurred_at TIMESTAMPTZ NOT NULL` (el mejor
  instante conocido del cobro: el extraído, o si falta, la fecha de RECEPCIÓN del mensaje) +
  `occurred_at_precision` (`datetime` = hora del cobro conocida; `date` = solo la fecha, hora a
  medianoche como placeholder; `inferred` = sin fecha, se usó la recepción). El dedup compara por HORA
  solo cuando ambos lados son `datetime`; si no, por DÍA. Estado como calendar (`processed_at`/etc.).
- `mod_finance_dedup_candidates` — pares candidatos de duplicado (FASE 1). Igual que calendar pero el
  par puede nacer ya `confirmed` con `decided_by='procedural'` cuando el score procedimental es alto;
  los de la banda ambigua quedan `candidate` para la FASE 2 LLM (`decided_by='llm'`).
- `mod_finance_consolidated` + `mod_finance_transaction_links` — proyección consolidada (union-find
  sobre los pares confirmados). A diferencia de calendar, el front lee finanzas desde el consolidado,
  así que la tabla guarda el payload completo de dominio. Sin ecos de proveedor ni conflictos.

Migración del dato existente: cada gasto viejo → transacción `direction='egreso'`, `counterparty :=
merchant`, `place := ''`, `occurred_at := COALESCE(occurred_on, recepción, created_at)`,
`date_inferred := (occurred_on IS NULL)`, `processing_outcome := 'unique'` (no se re-dedup en la
migración). Se DROPea el índice UNIQUE 0030 (finance ya no deduplica por business-key:
`identity_fields=()`, mecanismo propio) y la tabla `mod_finance_expenses` (camino limpio: relaciones,
API y front se actualizan en el mismo cambio). `downgrade` es LOSSY (se pierden direction/place y la
precisión de hora) — aceptable en el flujo forward-only del dueño.

Numeración (migration-numbering-worktrees): la cabeza commiteada es 0032; 0033 lo reclama el refactor
de identidad (`worktree-identidades-v2`, sin commitear). 0034 verificado libre en todas las ramas. Al
MERGEAR: si 0033_identidades ya está en main, re-apuntar `down_revision` a esa cabeza (linealizar; o
`alembic merge`) para no dejar multi-head.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0034"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Normalización de texto de la business-key 0030 (espejo de `memex.modules.dedup._NORM`), solo para
#: re-crear el índice viejo en `downgrade`.
_NORM = "lower(btrim(regexp_replace({x}, '\\s+', ' ', 'g')))"
_SENTINEL = "DATE '0001-01-01'"


def upgrade() -> None:
    # 1. Transacciones crudas (rename + reestructura de mod_finance_expenses).
    op.execute(
        """
        CREATE TABLE mod_finance_transactions (
            id                 BIGSERIAL PRIMARY KEY,
            user_id            BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_inbox_ids   BIGINT[] NOT NULL,
            direction          TEXT NOT NULL CHECK (direction IN ('ingreso','egreso')),
            amount             NUMERIC(14,2) NOT NULL,
            currency           TEXT NOT NULL,
            category           TEXT NOT NULL DEFAULT 'otros',
            counterparty       TEXT NOT NULL DEFAULT '',
            place              TEXT NOT NULL DEFAULT '',
            occurred_at        TIMESTAMPTZ NOT NULL,
            occurred_at_precision TEXT NOT NULL DEFAULT 'inferred'
                                 CHECK (occurred_at_precision IN ('datetime','date','inferred')),
            description        TEXT NOT NULL DEFAULT '',
            evidence           TEXT NOT NULL DEFAULT '',
            metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
            processed_at       TIMESTAMPTZ,
            processing_outcome TEXT NOT NULL DEFAULT 'pending'
                                 CHECK (processing_outcome IN ('pending','unique','duplicate')),
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX mod_finance_transactions_user_occurred
            ON mod_finance_transactions (user_id, occurred_at DESC);
        CREATE INDEX mod_finance_transactions_inbox_ids
            ON mod_finance_transactions USING GIN (source_inbox_ids);
        CREATE INDEX mod_finance_transactions_user_category
            ON mod_finance_transactions (user_id, category);
        CREATE INDEX mod_finance_transactions_user_outcome
            ON mod_finance_transactions (user_id, processing_outcome);
        """
    )

    # 2. Pares candidatos de dedup (FASE 1 procedimental + FASE 2 LLM).
    op.execute(
        """
        CREATE TABLE mod_finance_dedup_candidates (
            id               BIGSERIAL PRIMARY KEY,
            user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            transaction_a_id BIGINT NOT NULL
                               REFERENCES mod_finance_transactions(id) ON DELETE CASCADE,
            transaction_b_id BIGINT NOT NULL
                               REFERENCES mod_finance_transactions(id) ON DELETE CASCADE,
            reason           TEXT NOT NULL,
            score            NUMERIC(4,3),
            status           TEXT NOT NULL DEFAULT 'candidate'
                               CHECK (status IN ('candidate','confirmed','rejected')),
            decided_by       TEXT,
            confidence       NUMERIC(4,3),
            rationale        TEXT,
            decided_at       TIMESTAMPTZ,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (transaction_a_id < transaction_b_id),
            UNIQUE (transaction_a_id, transaction_b_id)
        );
        CREATE INDEX mod_finance_dedup_user_status
            ON mod_finance_dedup_candidates (user_id, status);
        """
    )

    # 3. Consolidación: transacción canónica + links N:1 hacia las crudas. Guarda el payload
    #    completo de dominio (el front lee finanzas desde acá).
    op.execute(
        """
        CREATE TABLE mod_finance_consolidated (
            id                    BIGSERIAL PRIMARY KEY,
            user_id               BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            direction             TEXT NOT NULL,
            amount                NUMERIC(14,2) NOT NULL,
            currency              TEXT NOT NULL,
            category              TEXT NOT NULL DEFAULT 'otros',
            counterparty          TEXT NOT NULL DEFAULT '',
            place                 TEXT NOT NULL DEFAULT '',
            occurred_at           TIMESTAMPTZ NOT NULL,
            occurred_at_precision TEXT NOT NULL DEFAULT 'inferred'
                                    CHECK (occurred_at_precision IN ('datetime','date','inferred')),
            description           TEXT NOT NULL DEFAULT '',
            winner_transaction_id BIGINT REFERENCES mod_finance_transactions(id) ON DELETE SET NULL,
            deleted               BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX mod_finance_consolidated_user_occurred
            ON mod_finance_consolidated (user_id, occurred_at DESC);

        CREATE TABLE mod_finance_transaction_links (
            id              BIGSERIAL PRIMARY KEY,
            user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            consolidated_id BIGINT NOT NULL
                              REFERENCES mod_finance_consolidated(id) ON DELETE CASCADE,
            transaction_id  BIGINT NOT NULL
                              REFERENCES mod_finance_transactions(id) ON DELETE CASCADE,
            UNIQUE (transaction_id)
        );
        CREATE INDEX mod_finance_transaction_links_consolidated
            ON mod_finance_transaction_links (consolidated_id);
        """
    )

    # 4. Backfill desde la tabla vieja: gasto → transacción egreso (fecha = la conocida, o recepción).
    op.execute(
        """
        INSERT INTO mod_finance_transactions
            (user_id, source_inbox_ids, direction, amount, currency, category, counterparty,
             place, occurred_at, occurred_at_precision, description, evidence, metadata,
             processed_at, processing_outcome, created_at)
        SELECT
            e.user_id, e.source_inbox_ids, 'egreso', e.amount, e.currency, e.category,
            e.merchant, '',
            COALESCE(
                e.occurred_on::timestamptz,
                (SELECT max(i.occurred_at) FROM inbox i WHERE i.id = ANY(e.source_inbox_ids)),
                e.created_at
            ),
            -- el gasto viejo solo tenía DATE: 'date' si la conocía, 'inferred' si cae a recepción.
            CASE WHEN e.occurred_on IS NULL THEN 'inferred' ELSE 'date' END,
            e.description, e.evidence, e.metadata,
            NOW(), 'unique', e.created_at
        FROM mod_finance_expenses e;
        """
    )

    # 5. Limpieza: el índice de business-key 0030 y la tabla vieja ya no se usan (finance es
    #    mecanismo-propio: identity_fields=()).
    op.execute("DROP INDEX IF EXISTS mod_finance_expenses_identity;")
    op.execute("DROP TABLE IF EXISTS mod_finance_expenses CASCADE;")


def downgrade() -> None:
    # Recrea la tabla vieja (shape 0008 + 0015 category) y copia de vuelta una proyección best-effort
    # (LOSSY: se pierden direction/place; occurred_at→occurred_on solo si NO fue inferida).
    op.execute(
        """
        CREATE TABLE mod_finance_expenses (
            id               BIGSERIAL PRIMARY KEY,
            user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_inbox_ids BIGINT[] NOT NULL,
            amount           NUMERIC(14,2) NOT NULL,
            currency         TEXT NOT NULL,
            category         TEXT NOT NULL DEFAULT 'otros',
            merchant         TEXT NOT NULL,
            occurred_on      DATE,
            description      TEXT NOT NULL DEFAULT '',
            evidence         TEXT NOT NULL DEFAULT '',
            metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX mod_finance_expenses_user_created
            ON mod_finance_expenses (user_id, created_at DESC);
        CREATE INDEX mod_finance_expenses_inbox_ids
            ON mod_finance_expenses USING GIN (source_inbox_ids);
        """
    )
    op.execute(
        """
        INSERT INTO mod_finance_expenses
            (user_id, source_inbox_ids, amount, currency, category, merchant, occurred_on,
             description, evidence, metadata, created_at)
        SELECT
            user_id, source_inbox_ids, amount, currency, category, counterparty,
            CASE WHEN occurred_at_precision = 'inferred' THEN NULL ELSE occurred_at::date END,
            description, evidence, metadata, created_at
        FROM mod_finance_transactions;
        """
    )
    # Colapsar duplicados por la business-key 0030 antes de re-crear el índice UNIQUE (si no, falla).
    fin_key = f"user_id, currency, amount, COALESCE(occurred_on, {_SENTINEL}), {_NORM.format(x='merchant')}"
    op.execute(
        f"""
        UPDATE mod_finance_expenses k SET source_inbox_ids = g.merged
        FROM (
            SELECT min(e.id) AS keep_id, array_agg(DISTINCT sid) AS merged
            FROM mod_finance_expenses e, unnest(e.source_inbox_ids) AS sid
            GROUP BY {fin_key}
        ) g
        WHERE k.id = g.keep_id;
        """
    )
    op.execute(
        f"""
        DELETE FROM mod_finance_expenses d USING (
            SELECT id, min(id) OVER (PARTITION BY {fin_key}) AS keep_id
            FROM mod_finance_expenses
        ) x
        WHERE d.id = x.id AND d.id <> x.keep_id;
        """
    )
    op.execute(
        f"CREATE UNIQUE INDEX mod_finance_expenses_identity ON mod_finance_expenses ({fin_key});"
    )

    op.execute("DROP TABLE IF EXISTS mod_finance_transaction_links CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_finance_consolidated CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_finance_dedup_candidates CASCADE;")
    op.execute("DROP TABLE IF EXISTS mod_finance_transactions CASCADE;")
