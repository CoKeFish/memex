"""finance: `status` (¿se movió la plata?) y `kind` (pago | reembolso) en crudas y consolidadas

Revision ID: 0090_finance_status_kind
Revises: 0089_asuntos_evidence_reviewed
Create Date: 2026-09-07

Auditoría 2026-09-06: la extracción registraba como transacción cosas que NO son plata que se
movió —pagos fallidos («payment was unsuccessful»), saldos pendientes por pagar, facturas aún no
pagadas— y las devoluciones (nota crédito, «devolución de la compra») como un INGRESO más, que
inflaba «lo que entró» en vez de descontar el gasto original. Pedirle al LLM que las OMITA no fue
confiable (el prompt ya lo decía); pedirle que las CLASIFIQUE sí lo es. Regla general:

- `status`: `completada` (la plata se movió; default) | `fallida` (cobro rechazado/declinado) |
  `pendiente` (por pagar: factura, saldo, recordatorio). Solo `completada` cuenta en totales; las
  otras se conservan con su estado (captura + perilla) para trazabilidad y para los asuntos.
- `kind`: `pago` (movimiento normal; default) | `reembolso` (plata que VUELVE de un cargo previo:
  devolución, nota crédito, reversión, chargeback). Un reembolso es `direction='ingreso'` pero NO
  es un ingreso real: descuenta del gasto en su rubro.

El consolidado hereda ambos del ganador: la FASE 1 del dedup ya no empareja filas con
status/kind/direction distintos (un intento fallido y el cobro que después sí pasó son dos hechos).

Numeración (migration-numbering-worktrees): head lineal = 0089; los otros worktrees/ramas vivos
(relevancia-gate-regex en 0077, exp/identidades-calidad en 0082) no usan 0090 → libre.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0090_finance_status_kind"
down_revision: str | None = "0089_asuntos_evidence_reviewed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS = (
    "TEXT NOT NULL DEFAULT 'completada' CHECK (status IN ('completada', 'fallida', 'pendiente'))"
)
_KIND = "TEXT NOT NULL DEFAULT 'pago' CHECK (kind IN ('pago', 'reembolso'))"


def upgrade() -> None:
    for table in ("mod_finance_transactions", "mod_finance_consolidated"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN status {_STATUS}")
        op.execute(f"ALTER TABLE {table} ADD COLUMN kind {_KIND}")


def downgrade() -> None:
    for table in ("mod_finance_transactions", "mod_finance_consolidated"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS kind")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS status")
