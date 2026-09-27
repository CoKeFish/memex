"""Suscripciones: el compromiso recurrente de pago a un proveedor (finanzas)

Revision ID: 0101_finance_subscriptions
Revises: 0100_calendar_vault_token
Create Date: 2026-09-12

Finanzas guardaba cargos sueltos: nada decía «esto se repite cada mes» ni «se renueva el 1». Una
SUSCRIPCIÓN no es una transacción, es la REGLA que las genera (proveedor + cadencia + monto típico
+ estado), así que va en su propia tabla y es un vértice del grafo, con sus cargos como evidencia.
Vive en finanzas por decisión ya tomada (ver `modules/asuntos/assign.py`: una renovación ordinaria
no es un asunto; dar de alta un servicio es una transacción de finanzas).

`recurrence` usa las MISMAS líneas RFC 5545 que el calendario (`modules/calendar/recurrence.py`,
motor puro y reutilizable): una sola regla, sin materializar ocurrencias. `next_renewal_on` sí se
materializa porque es lo que se lee y por lo que se avisa.

`provider_key` es la clave natural (idempotencia del detector): la identidad del proveedor cuando
el directorio la resolvió (`ident:<id>`), o el nombre normalizado si no (`name:<texto>`). Junto con
la moneda separa "Nubix en USD" de "Nubix en COP".

`pinned` protege lo que el usuario corrigió a mano: el detector no lo pisa (mismo patrón que
`protected` en calendario). Los cargos reales son ruidosos y la ingesta tiene huecos, así que el
detector propone con `confidence` y el usuario confirma o descarta; `status='descartada'` no se
vuelve a proponer.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0101_finance_subscriptions"
down_revision: str | None = "0100_calendar_vault_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mod_finance_subscriptions (
            id                   BIGSERIAL PRIMARY KEY,
            user_id              BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            -- Proveedor: texto visible + identidad del directorio cuando se pudo resolver.
            provider             TEXT NOT NULL,
            provider_identity_id BIGINT REFERENCES mod_identidades(id) ON DELETE SET NULL,
            -- Clave natural del detector (ident:<id> | name:<normalizado>).
            provider_key         TEXT NOT NULL,
            currency             TEXT NOT NULL,
            -- Monto habitual (mediana de los cargos). `amount_varies` = cobra por uso (Nubix) y el
            -- monto es solo una referencia, no una promesa.
            amount_typical       NUMERIC(14,2),
            amount_varies        BOOLEAN NOT NULL DEFAULT FALSE,
            -- Regla de renovación en RFC 5545, igual que el calendario. Vacío = se conoce el
            -- proveedor pero todavía no la cadencia.
            recurrence           TEXT[] NOT NULL DEFAULT '{}',
            first_charge_on      DATE,
            last_charge_on       DATE,
            next_renewal_on      DATE,
            -- activa | sin_cobros (venció el plazo sin cargo: quizá cancelada) | cancelada |
            -- descartada (el usuario dice que no era una suscripción; el detector no la repropone)
            status               TEXT NOT NULL DEFAULT 'activa',
            -- De dónde salió: inferida (periodicidad de los cargos) | declarada (lo dijo un
            -- mensaje) | manual (la cargó el usuario).
            source               TEXT NOT NULL DEFAULT 'inferida',
            confidence           REAL NOT NULL DEFAULT 0,
            charges              INTEGER NOT NULL DEFAULT 0,
            -- Por qué el detector la propuso, en una línea legible.
            evidence             TEXT NOT NULL DEFAULT '',
            notes                TEXT NOT NULL DEFAULT '',
            pinned               BOOLEAN NOT NULL DEFAULT FALSE,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT mod_finance_subscriptions_status_check
                CHECK (status IN ('activa', 'sin_cobros', 'cancelada', 'descartada')),
            CONSTRAINT mod_finance_subscriptions_source_check
                CHECK (source IN ('inferida', 'declarada', 'manual')),
            CONSTRAINT mod_finance_subscriptions_confidence_check
                CHECK (confidence >= 0 AND confidence <= 1),
            UNIQUE (user_id, provider_key, currency)
        );
        CREATE INDEX mod_finance_subscriptions_next
            ON mod_finance_subscriptions (user_id, next_renewal_on)
            WHERE status <> 'cancelada';
        CREATE INDEX mod_finance_subscriptions_identity
            ON mod_finance_subscriptions (provider_identity_id)
            WHERE provider_identity_id IS NOT NULL;

        -- Evidencia: qué cargos sostienen la suscripción. Un cargo pertenece a lo sumo a UNA
        -- (UNIQUE), así no se cuenta dos veces en los totales.
        CREATE TABLE mod_finance_subscription_charges (
            subscription_id BIGINT NOT NULL
                              REFERENCES mod_finance_subscriptions(id) ON DELETE CASCADE,
            consolidated_id BIGINT NOT NULL
                              REFERENCES mod_finance_consolidated(id) ON DELETE CASCADE,
            PRIMARY KEY (subscription_id, consolidated_id),
            UNIQUE (consolidated_id)
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS mod_finance_subscription_charges;
        DROP TABLE IF EXISTS mod_finance_subscriptions;
        """
    )
