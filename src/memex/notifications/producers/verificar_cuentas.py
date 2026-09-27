"""Productor `verificar_cuentas`: revalida solo las credenciales de las cuentas (`sistema`).

No redacta avisos: PRUEBA contra el proveedor (login IMAP, refresh del token OAuth, sesión de
Telegram…) las cuentas cuya evidencia de salud tiene más de `cada_horas`, y persiste el resultado.
Si una falla, la alerta la levanta `memex.core.alerts` (cuenta `unhealthy`) y la emite el productor
`sistema`, con un aviso por cuenta y por día mientras siga rota. Así una cuenta caída se ve en la
tarjeta de /cuenta, en la campana y por el canal (Telegram) sin que nadie toque «Validar».

Único productor que ejecuta I/O de red y abre sus propias conexiones (vía `check_account`); por
eso no toca `ctx.conn` más que para listar qué cuentas tocan.
"""

from __future__ import annotations

from memex.core.account_health import accounts_due_for_check, check_account
from memex.logging import get_logger
from memex.notifications.producers import Draft, ProducerContext, ProducerSpec

_log = get_logger("memex.notifications.producers.verificar_cuentas")

NAME = "verificar_cuentas"


async def produce(ctx: ProducerContext) -> list[Draft]:
    due = accounts_due_for_check(
        ctx.conn, ctx.user_id, older_than_hours=int(ctx.config["cada_horas"]), now=ctx.now
    )
    for account_id in due:
        try:
            result = await check_account(account_id, ctx.user_id)
        except Exception as e:  # una cuenta mal configurada no frena a las demás
            _log.warning(
                "notifications.verificar_cuentas.failed", account_id=account_id, error=str(e)[:300]
            )
            continue
        if result is not None:
            _log.info(
                "notifications.verificar_cuentas.checked",
                account_id=account_id,
                status=result.status,
                code=result.code,
            )
    return []


SPEC = ProducerSpec(
    name=NAME,
    audience="sistema",
    description="Revalida las credenciales de las cuentas cada N horas; fallos → `sistema`",
    default_enabled=True,
    default_render="plantilla",
    default_config={"cada_horas": 6},
    produce=produce,
    llm_allowed=False,
)
