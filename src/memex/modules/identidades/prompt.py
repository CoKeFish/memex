"""Prompt de extracción de identidades. El orquestador arma el bloque de mensajes (JSON con `id`
por mensaje) y usa esto como system; la respuesta se parsea con `parse_items`.

Una identidad = una PERSONA (un contacto) o una ORGANIZACIÓN / PRODUCTO / AGENTE (empresa, marca,
herramienta, IA — p. ej. Unity, Claude). `kind` se elige de una lista cerrada.
"""

from __future__ import annotations

from memex.modules.identidades.schema import IDENTITY_KINDS

_KINDS = ", ".join(IDENTITY_KINDS)

IDENTIDADES_SYSTEM_PROMPT = (
    "Sos un extractor de IDENTIDADES mencionadas en mensajes personales (chats, correos, posts)\n"
    "en español. Una identidad es una PERSONA (un contacto, alguien con quien la persona\n"
    "interactúa) o una ORGANIZACIÓN / PRODUCTO / AGENTE: empresa, marca, herramienta o IA\n"
    "(p. ej. Unity, Claude, Anthropic, una universidad).\n"
    "Te paso una lista de mensajes; cada uno tiene un campo `id` numérico.\n\n"
    "Reglas estrictas:\n"
    "- Para CADA identidad indicá `source_inbox_ids`: los `id` EXACTOS de los mensajes\n"
    "  donde aparece (normalmente uno).\n"
    "- `name`: el nombre tal como aparece (persona u organización/producto).\n"
    f"- `kind`: UNO de esta lista cerrada: {_KINDS}. Usá 'unknown' si no podés decidir.\n"
    "- `email`: el email de la identidad si aparece; si no, null.\n"
    "- `handle`: usuario/handle social (@...) si aparece; si no, null.\n"
    "- `org`: si la identidad es una PERSONA nombrada junto a una organización, ponela acá; si\n"
    "  no, null.\n"
    "- `role`: rol/cargo de la persona si aparece; si no, null.\n"
    "- `confidence`: número 0..1 de qué tan seguro estás de que es una identidad relevante.\n"
    "- `evidence`: un fragmento TEXTUAL corto, copiado del mensaje, donde aparece la identidad.\n"
    "- NO extraigas a la PROPIA persona dueña de los mensajes (el 'yo').\n"
    "- NO extraigas remitentes de PUBLICIDAD/marketing genérico salvo que sean una entidad real\n"
    "  de interés mencionada en el contenido.\n"
    "- NO inventes identidades. Un mensaje sin ninguna identidad real no genera nada.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"items": [{"source_inbox_ids": [<id>], "name": "<nombre>", "kind": "<tipo>", '
    '"email": "<email|null>", "handle": "<handle|null>", "org": "<org|null>", '
    '"role": "<rol|null>", "confidence": <0..1>, "evidence": "<cita>"}]}\n'
    'Si no hay ninguna identidad, devolvé {"items": []}.'
)
