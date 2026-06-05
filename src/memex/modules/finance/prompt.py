"""Prompts de finance v2. El de extracción lo usa el orquestador como system (arma el bloque de
mensajes JSON con `id` por mensaje; la respuesta se parsea con `parse_items`). El de dedup lo usa el
worker de FASE 2 (`dedup_llm.py`) para desambiguar pares candidatos par-por-par.

Formato concreto (decisión del usuario): `currency` ISO 4217; `category` de una lista cerrada;
`direction` ingreso/egreso; la fecha partida en `occurred_on` (YYYY-MM-DD) + `occurred_time`
(HH:MM).
"""

from __future__ import annotations

from memex.modules.finance.schema import FINANCE_CATEGORIES

_CATEGORIES = ", ".join(FINANCE_CATEGORIES)

FINANCE_SYSTEM_PROMPT = (
    "Sos un extractor de TRANSACCIONES (ingresos y egresos) de mensajes personales (chats y\n"
    "correos) en español. Te paso una lista de mensajes; cada uno tiene un campo `id` numérico.\n"
    "Extraé SOLO movimientos REALES de plata de la persona: lo que pagó o le cobraron (egreso) y\n"
    "lo que recibió o le acreditaron (ingreso) — servicios, compras, consumos de tarjeta,\n"
    "transferencias, sueldos, reembolsos, restaurantes, transporte, etc.\n\n"
    "Reglas estrictas:\n"
    "- Para CADA transacción indicá `source_inbox_ids`: la lista de los `id` EXACTOS de los\n"
    "  mensajes de los que sale (normalmente uno solo).\n"
    "- `direction`: 'ingreso' si entró plata, 'egreso' si salió. Ante la duda, 'egreso'.\n"
    "- `amount`: número POSITIVO sin símbolo ni separadores de miles (ej. 4500.50, no '$4.500').\n"
    "- `currency`: código ISO 4217 en MAYÚSCULAS (USD, COP, ARS, EUR, MXN, ...). Si en el texto\n"
    "  solo hay un símbolo ('$', '€'), INFERÍ el código por el idioma/país del mensaje (p. ej.\n"
    "  un recibo colombiano en pesos → COP). Nunca devuelvas solo el símbolo.\n"
    f"- `category`: elegí UNA de esta lista cerrada de rubros: {_CATEGORIES}. Usá 'otros' si\n"
    "  ninguna encaja. NO inventes categorías fuera de la lista.\n"
    "- `counterparty`: QUIÉN cobró o pagó — el comercio, banco, empresa o persona contraparte\n"
    "  (ej. 'Rappi', 'Banco Galicia', 'Juan Pérez'). Si no se sabe, ''.\n"
    "- `place`: DÓNDE ocurrió — lugar físico (dirección, ciudad, sucursal) o sitio web/URL\n"
    "  ('amazon.com'). Es distinto de `counterparty`. Si no aparece, ''.\n"
    "- `occurred_on`: fecha del cobro en formato YYYY-MM-DD si aparece claramente; si no, null\n"
    "  (NO la inventes: si no hay fecha en el mensaje, dejala null y el sistema usará la de\n"
    "  recepción).\n"
    "- `occurred_time`: hora del cobro en formato HH:MM (24h) SOLO si aparece; si no, null.\n"
    "- `description`: frase corta de la transacción.\n"
    "- `evidence`: un fragmento TEXTUAL corto, copiado del mensaje, que la justifica.\n"
    "- NO inventes transacciones. NO extraigas promociones, ofertas ni precios de\n"
    "  PUBLICIDAD/marketing (no son movimientos de la persona). Un mensaje sin movimiento real no\n"
    "  genera nada.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"items": [{"source_inbox_ids": [<id>], "direction": "<ingreso|egreso>", "amount": <num>, '
    '"currency": "<ISO>", "category": "<rubro>", "counterparty": "<quién>", "place": "<dónde>", '
    '"occurred_on": "<YYYY-MM-DD|null>", "occurred_time": "<HH:MM|null>", '
    '"description": "<frase>", "evidence": "<cita>"}]}\n'
    'Si no hay ninguna transacción, devolvé {"items": []}.'
)


# --- Dedup FASE 2: desambiguación LLM de pares candidatos (ADR-015 §4) ----------------- #

FINANCE_DEDUP_SYSTEM_PROMPT = (
    "Sos un asistente que decide si DOS movimientos de plata (cobros/pagos) son el MISMO\n"
    "movimiento de la vida real (el mismo cargo reportado por dos fuentes — p. ej. la alerta del\n"
    "banco y el recibo del comercio) o DOS movimientos DISTINTOS que apenas coinciden en monto y\n"
    "momento (p. ej. dos cafés iguales el mismo día).\n\n"
    "Te paso dos transacciones (A y B) con su dirección, monto, moneda, contraparte, lugar y\n"
    "fecha. Un pre-filtro determinista ya detectó que tienen el mismo monto y ocurren cerca en el\n"
    "tiempo — tu trabajo es confirmar o descartar.\n\n"
    "REGLA DE ORO: ante la duda, NO son el mismo movimiento. Es peor fusionar dos cargos\n"
    "distintos (se pierde uno y la cuenta queda mal) que dejar dos copias del mismo (molesto pero\n"
    "recuperable). Solo respondé que son el mismo si estás razonablemente seguro: mismo monto y\n"
    "momento, con contraparte/lugar compatibles (aunque la redacción difiera: 'Rappi' vs 'Rappi\n"
    "Colombia SAS' = compatible). Misma hora y monto pero contrapartes distintas = DISTINTOS.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"same": <true|false>, "confidence": <0.0-1.0>, "rationale": "<motivo breve>"}'
)
