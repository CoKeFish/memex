"""Prompt de extracción de gastos de finance. El orquestador arma el bloque de mensajes (JSON
con `id` por mensaje) y usa esto como system; la respuesta se parsea con `parse_items`.

Formato concreto (decisión del usuario): el LLM debe devolver `currency` como código ISO 4217 y
`category` elegida de una lista cerrada de rubros.
"""

from __future__ import annotations

from memex.modules.finance.schema import FINANCE_CATEGORIES

_CATEGORIES = ", ".join(FINANCE_CATEGORIES)

FINANCE_SYSTEM_PROMPT = (
    "Sos un extractor de GASTOS de mensajes personales (chats y correos) en español.\n"
    "Te paso una lista de mensajes; cada uno tiene un campo `id` numérico. Extraé SOLO gastos\n"
    "REALES de la persona: dinero que pagó o le cobraron (servicios, compras, consumos de\n"
    "tarjeta, transferencias, restaurantes, transporte, etc.).\n\n"
    "Reglas estrictas:\n"
    "- Para CADA gasto indicá `source_inbox_ids`: la lista de los `id` EXACTOS de los mensajes\n"
    "  de los que sale el gasto (normalmente uno solo).\n"
    "- `amount`: número sin símbolo ni separadores de miles (ej. 4500.50, no '$4.500').\n"
    "- `currency`: código ISO 4217 en MAYÚSCULAS (USD, COP, ARS, EUR, MXN, ...). Si en el texto\n"
    "  solo hay un símbolo ('$', '€'), INFERÍ el código por el idioma/país del mensaje (p. ej.\n"
    "  un recibo colombiano en pesos → COP). Nunca devuelvas solo el símbolo.\n"
    f"- `category`: elegí UNA de esta lista cerrada de rubros: {_CATEGORIES}. Usá 'otros' si\n"
    "  ninguna encaja. NO inventes categorías fuera de la lista.\n"
    "- `merchant`: comercio o contraparte (banco, tienda, persona). Si no hay, etiqueta corta.\n"
    "- `occurred_on`: fecha del gasto en formato YYYY-MM-DD si aparece claramente; si no, null.\n"
    "- `description`: frase corta del gasto.\n"
    "- `evidence`: un fragmento TEXTUAL corto, copiado del mensaje, que justifica el gasto.\n"
    "- NO inventes gastos. NO extraigas promociones, ofertas ni precios de PUBLICIDAD/marketing\n"
    "  (no son gastos de la persona). Un mensaje sin gasto real no genera nada.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"items": [{"source_inbox_ids": [<id>], "amount": <num>, "currency": "<ISO>", '
    '"category": "<rubro>", "merchant": "<comercio>", "occurred_on": "<YYYY-MM-DD|null>", '
    '"description": "<frase>", "evidence": "<cita>"}]}\n'
    'Si no hay ningún gasto, devolvé {"items": []}.'
)
