"""Prompts de finance v2. El de extracción lo usa el orquestador como system (arma el bloque de
mensajes JSON con `id` por mensaje; la respuesta se parsea con `parse_items`). El de dedup lo usa el
worker de FASE 2 (`dedup_llm.py`) para desambiguar pares candidatos par-por-par.

Formato concreto (decisión del usuario): `currency` ISO 4217; `category` de una lista cerrada;
`direction` ingreso/egreso; `status` completada/fallida/pendiente; `kind` pago/reembolso; la fecha
partida en `occurred_on` (YYYY-MM-DD) + `occurred_time` (HH:MM).

Por qué `status`/`kind` son campos y no una instrucción de «ignorá esto» (2026-09-07): el prompt
anterior pedía omitir los pagos fallidos y el LLM igual los extraía como egresos; y una nota
crédito/devolución salía como un ingreso más. Clasificar es más confiable que suprimir: el modelo
declara qué es cada movimiento y el sistema decide qué cuenta (solo `completada`) y cómo suma (un
`reembolso` descuenta del gasto, no infla lo que entró).
"""

from __future__ import annotations

from memex.modules.finance.rubros import CATEGORY_GUIDE
from memex.modules.finance.schema import FINANCE_CATEGORIES

_CATEGORIES = ", ".join(FINANCE_CATEGORIES)

FINANCE_SYSTEM_PROMPT = (
    "Sos un extractor de TRANSACCIONES (ingresos y egresos) de mensajes personales (chats y\n"
    "correos) en español. Te paso una lista de mensajes; cada uno tiene un campo `id` numérico.\n"
    "Extraé los movimientos de plata de la persona: lo que pagó o le cobraron (egreso) y lo\n"
    "que recibió o le acreditaron (ingreso) — servicios, compras, consumos de tarjeta,\n"
    "transferencias, sueldos, reembolsos, restaurantes, transporte, etc. También registrá los\n"
    "intentos de cobro que NO se concretaron y las cuentas por pagar, pero MARCÁNDOLOS con su\n"
    "`status` (ver abajo): el sistema decide qué cuenta y qué no; tu trabajo es clasificar\n"
    "bien, no omitir.\n\n"
    "Reglas estrictas:\n"
    "- Para CADA transacción indicá `source_inbox_ids`: la lista de los `id` EXACTOS de los\n"
    "  mensajes de los que sale (normalmente uno solo).\n"
    "- `direction`: 'ingreso' si entró plata, 'egreso' si salió. Ante la duda, 'egreso'.\n"
    "- `status`: ¿la plata efectivamente se MOVIÓ?\n"
    "    'completada' → el cobro/pago se realizó (recibo, «pago aprobado», «te cobramos», factura\n"
    "      electrónica de una compra ya hecha, consumo de tarjeta).\n"
    "    'fallida' → el cobro se intentó y NO pasó: «payment failed/unsuccessful», «pago\n"
    "      rechazado/declinado/no procesado», «no pudimos cobrar», «intento de cobro fallido».\n"
    "    'pendiente' → todavía NO se pagó: factura emitida por pagar, «saldo pendiente», «paga\n"
    "      antes de», recordatorio de pago, cuota por vencer.\n"
    "  Un mensaje puede traer solo un cobro fallido (status 'fallida'): igual listalo.\n"
    "- `kind`: 'pago' para un movimiento normal. 'reembolso' cuando la plata VUELVE de un cargo\n"
    "  anterior: devolución, reembolso, nota crédito, reversión, contracargo/chargeback. Un\n"
    "  reembolso lleva direction 'ingreso' (la plata entra) pero kind 'reembolso' (no es un\n"
    "  ingreso real: descuenta el gasto original). Usá el rubro del gasto que se devuelve.\n"
    "- `amount`: número POSITIVO, SIN separadores de miles (ni '.' ni ','). OJO: en español '.' o\n"
    "  ',' casi siempre son separador de MILES: '3.925' y '3,925' = 3925, NO 3.925. Usá '.' SOLO\n"
    "  como decimal de centavos reales (ej. 2.54); si la moneda no usa centavos (COP/CLP/ARS)\n"
    "  devolvé ENTERO. Sin símbolo de moneda. Ej.: '3.925 COP' = 3925 ; 'USD 2.54' = 2.54.\n"
    "  Si el mensaje NO dice el monto (o dice $0), NO hay transacción: no la listes.\n"
    "- `currency`: código ISO 4217 en MAYÚSCULAS (USD, COP, ARS, EUR, MXN, ...). Si en el texto\n"
    "  solo hay un símbolo ('$', '€'), INFERÍ el código por el idioma/país del mensaje (p. ej.\n"
    "  un recibo colombiano en pesos → COP). Nunca devuelvas solo el símbolo.\n"
    f"- `category`: elegí UNA de esta lista cerrada de rubros: {_CATEGORIES}. NO inventes\n"
    "  categorías fuera de la lista. Qué es cada uno:\n"
    f"{CATEGORY_GUIDE}\n"
    "  'otros' es SOLO para cuando no se puede saber qué es el comercio ni el motivo; si el\n"
    "  nombre del comercio o la plataforma es reconocible, clasificalo.\n"
    "- `counterparty`: QUIÉN facturó o recibió realmente el dinero — el comercio, la empresa o\n"
    "  la persona con la que se hizo el movimiento (ej. '<comercio>', '<plataforma>', 'Juan "
    "Pérez'). OJO:\n"
    "  quien REPORTA el movimiento NO es necesariamente la contraparte. Si un banco o tarjeta\n"
    "  avisa de un consumo hecho en un comercio, la contraparte es el COMERCIO, no el banco (es\n"
    "  solo el canal/remitente). El banco SÍ es la contraparte cuando el cobro es suyo (comisión,\n"
    "  mantenimiento, intereses). Lo mismo aplica a una factura reenviada o un texto a mano: la\n"
    "  contraparte es quien facturó, no quien mandó el mensaje. Si no se sabe, ''.\n"
    "- `place`: DÓNDE ocurrió — lugar físico (dirección, ciudad, sucursal) o sitio web/URL\n"
    "  ('amazon.com'). Es distinto de `counterparty`. Si no aparece, ''.\n"
    "- `occurred_on`: fecha del cobro en formato YYYY-MM-DD si aparece claramente; si no, null\n"
    "  (NO la inventes: si no hay fecha en el mensaje, dejala null y el sistema usará la de\n"
    "  recepción).\n"
    "- `occurred_time`: hora del cobro en formato HH:MM (24h) SOLO si aparece; si no, null.\n"
    "- `occurred_tz`: SOLO si el mensaje dice EXPLÍCITAMENTE la zona horaria de esa hora (ej.\n"
    "  '10:00 UTC', '3:00 PM GMT-5', 'hora de Colombia'): el desplazamiento como '±HH:MM' o\n"
    "  'UTC' (ej. 'UTC', '-05:00', '+01:00'). Si no la dice, null: se asume la hora local de la\n"
    "  persona. NO deduzcas la zona por el país del remitente.\n"
    "- `description`: frase corta de la transacción.\n"
    "- `evidence`: un fragmento TEXTUAL corto, copiado del mensaje, que la justifica.\n"
    "- NO inventes transacciones. NO extraigas promociones, ofertas ni precios de\n"
    "  PUBLICIDAD/marketing (no son movimientos de la persona). Un mensaje sin movimiento, sin\n"
    "  intento de cobro y sin cuenta por pagar no genera nada.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"items": [{"source_inbox_ids": [<id>], "direction": "<ingreso|egreso>", '
    '"status": "<completada|fallida|pendiente>", "kind": "<pago|reembolso>", "amount": <num>, '
    '"currency": "<ISO>", "category": "<rubro>", "counterparty": "<quién>", "place": "<dónde>", '
    '"occurred_on": "<YYYY-MM-DD|null>", "occurred_time": "<HH:MM|null>", '
    '"occurred_tz": "<UTC|±HH:MM|null>", '
    '"description": "<frase>", "evidence": "<cita>"}]}\n'
    'Si no hay ninguna transacción, devolvé {"items": []}.'
)


# --- Dedup FASE 2: desambiguación LLM de pares candidatos (ADR-015 §4) ----------------- #

FINANCE_DEDUP_SYSTEM_PROMPT = (
    "Sos un asistente que decide si DOS movimientos de plata (cobros/pagos) son el MISMO\n"
    "movimiento de la vida real (el mismo cargo reportado por dos fuentes — p. ej. la alerta del\n"
    "banco y el recibo del comercio, o el extracto del banco y el correo del comercio) o DOS\n"
    "movimientos DISTINTOS que apenas coinciden en monto y momento (p. ej. dos cafés iguales el\n"
    "mismo día).\n\n"
    "Te paso dos transacciones (A y B) con su dirección, monto, moneda, contraparte, lugar, fecha\n"
    "y, entre corchetes, el TIPO de fuente de cada una (correo con su remitente, chat, documento\n"
    "aportado como un extracto bancario, registro del agente). Un pre-filtro determinista ya\n"
    "detectó que tienen el mismo monto (o un monto EQUIVALENTE en otra moneda — p. ej. el banco\n"
    "lo reporta en pesos y el comercio factura en dólares; en ese caso te doy la conversión\n"
    "aproximada) y ocurren cerca en el tiempo — tu trabajo es confirmar o descartar.\n\n"
    "FUENTES INDEPENDIENTES: un extracto bancario lista TODOS los movimientos de la cuenta en su\n"
    "período, así que un cobro avisado por correo dentro de ese período casi siempre aparece\n"
    "también en el extracto. Mismo monto, mismo día y misma naturaleza en un extracto y en un\n"
    "correo es, por defecto, el MISMO movimiento. Lo mismo vale para dos correos de REMITENTES\n"
    "distintos que cuentan el mismo pago desde dos lados: el aviso del banco o de la pasarela y\n"
    "el recibo del comercio. Ojo con la contraparte en esos casos: el lado del banco o de la\n"
    "pasarela suele nombrar al procesador de pagos, a la red de recaudo o un rótulo genérico\n"
    "('compra en <plataforma de pagos>'), mientras que el comercio se nombra a sí mismo. Esa\n"
    "diferencia de nombre NO los hace distintos, y tampoco que lo describan con otras palabras\n"
    "('compra de paquete' o 'recarga' en el extracto vs 'pago de factura' en el aviso del\n"
    "operador: es el mismo cobro descrito por dos sistemas).\n\n"
    "MONTOS REDONDOS: en muchos países casi todo es redondo (9.000, 13.000, 160.000), así que un\n"
    "monto redondo por sí solo NO resta. Sé más exigente solo cuando ADEMÁS las contrapartes son\n"
    "distintas y sin relación aparente (p. ej. giros del mismo monto a dos personas distintas).\n"
    "Si la contraparte coincide aunque un lado la abrevie o la escriba distinto, si un lado es un\n"
    "intermediario plausible del otro, o si un lado es la factura/recibo y el otro la\n"
    "transferencia o el pago que la saldó el mismo día, son el MISMO movimiento aunque no\n"
    "compartan un número de referencia.\n\n"
    "REGLA DE ORO: ante la duda, NO son el mismo movimiento. Es peor fusionar dos cargos\n"
    "distintos (se pierde uno y la cuenta queda mal) que dejar dos copias del mismo (molesto pero\n"
    "recuperable). Solo respondé que son el mismo si estás razonablemente seguro: mismo monto (o\n"
    "equivalente convertido, tolerando el spread de la tasa) y momento, con contraparte/lugar\n"
    "compatibles (aunque la redacción difiera: '<marca>' vs '<marca> Colombia SAS' = compatible).\n"
    "Entre avisos del MISMO remitente (dos alertas del mismo banco, dos recibos del mismo\n"
    "comercio), misma hora y monto pero contrapartes distintas = DISTINTOS.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"same": <true|false>, "confidence": <0.0-1.0>, "rationale": "<motivo breve>"}'
)
