"""identidades: resolution_method 'sender' (remitente de primera clase, Fase 2)

Revision ID: 0070_sender_resolution_method
Revises: 0069_relation_edge_axes
Create Date: 2026-06-14

Fase 2 del refactor del grafo: el REMITENTE de un mensaje se vuelve identidad de PRIMERA CLASE —
resuelto y persistido como avistamiento en la extracción (paso 5), uniforme entre medios (chat /
email / social). El avistamiento entra a `mod_identidades_mentions` con un `resolution_method`
propio para distinguirlo de una mención del cuerpo: así co-ocurre con lo extraído del mensaje por
el brazo NORMAL de menciones (antes la co-ocurrencia del remitente se DERIVABA al vuelo en
`relations/cooccurrence.py::_SENDER_PROVENANCE_SQL`, eliminado en esta fase).

Se AÑADE el valor `'sender'` al CHECK de `resolution_method` (el viejo `'sender_email'` de 0033
—vestigial, nunca escrito— se conserva para no tocar el contrato existente). Único valor uniforme
entre medios: un remitente de chat es una persona (no un email), por eso `'sender'` y no
`'sender_email'`.

El CHECK de 0033 es inline sin nombre → autonombre de Postgres
`mod_identidades_mentions_resolution_method_check` (mismo patrón que 0057 usó para `resolved_kind`).

Numeración (migration-numbering-worktrees): 0070 verificado libre en main + worktrees
(desmontar-build-relations, resolve-pistas); head lineal = 0069_relation_edge_axes.

DOWNGRADE: re-estrecha el CHECK a los valores de 0033 (incl. 'sender_email'). Es lossy si quedaron
filas con method='sender': se migran a 'unresolved' ANTES de re-estrechar (no perder la mención,
solo su método).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0070_sender_resolution_method"
down_revision: str | None = "0069_relation_edge_axes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_METHODS_WITH_SENDER = (
    "'email','handle','exact_name','alias','domain','created',"
    "'unresolved','fuzzy','llm','sender_email','sender'"
)
_METHODS_0033 = (
    "'email','handle','exact_name','alias','domain','created',"
    "'unresolved','fuzzy','llm','sender_email'"
)


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE mod_identidades_mentions
            DROP CONSTRAINT IF EXISTS mod_identidades_mentions_resolution_method_check;
        ALTER TABLE mod_identidades_mentions
            ADD CONSTRAINT mod_identidades_mentions_resolution_method_check
            CHECK (resolution_method IN ({_METHODS_WITH_SENDER}));
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE mod_identidades_mentions SET resolution_method = 'unresolved'
            WHERE resolution_method = 'sender';
        ALTER TABLE mod_identidades_mentions
            DROP CONSTRAINT IF EXISTS mod_identidades_mentions_resolution_method_check;
        ALTER TABLE mod_identidades_mentions
            ADD CONSTRAINT mod_identidades_mentions_resolution_method_check
            CHECK (resolution_method IN ({_METHODS_0033}));
        """
    )
