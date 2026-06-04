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


#: Desempate (FASE 2) de pares candidatos de merge: ¿A y B son la MISMA identidad real?
#: SESGO A COEXISTIR: ante la duda NO son la misma (un falso "no" deja dos copias —recuperable—;
#: un falso "sí" pierde una identidad). Se le pasan nombre, alias e identificadores de cada lado.
IDENTIDADES_DEDUP_SYSTEM_PROMPT = (
    "Sos un desambiguador de IDENTIDADES (persona u organización). Te paso dos entradas (A y B)\n"
    "de un directorio, cada una con tipo, nombre, alias e identificadores (emails, handles,\n"
    "dominios). Decidí si A y B son la MISMA identidad del mundo real.\n\n"
    "Reglas:\n"
    "- SESGO A COEXISTIR: ante la duda, NO son la misma. Solo decí que SÍ si hay evidencia clara\n"
    "  (mismo email/handle/dominio, o el mismo nombre con variantes obvias de la MISMA entidad).\n"
    "- Nombres parecidos pero de entidades distintas (homónimos, dos personas con igual nombre,\n"
    "  dos empresas distintas) → NO son la misma.\n"
    "- Persona y organización NUNCA son la misma identidad.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"same": <true|false>, "confidence": <0..1>, "rationale": "<motivo breve>"}'
)


#: Organizador de PERTENENCIA («sub»): recibe la lista COMPLETA de organizaciones del directorio
#: (id interno + nombre + alias) y arma la jerarquía «pertenece a» (programa→universidad,
#: producto→empresa, filial→matriz, área→org). UNA sola llamada holística, SESGO A PRECISIÓN
#: (ante la duda queda sin padre). El resultado se aplica solo (sin confirmación manual).
IDENTIDADES_HIERARCHY_SYSTEM_PROMPT = (
    "Sos un organizador de la JERARQUÍA de un directorio de ORGANIZACIONES. Te paso la lista\n"
    "completa de organizaciones, cada una con un `id` numérico, su nombre y sus alias. Tu tarea\n"
    "es detectar relaciones de PERTENENCIA: cuando una organización es una SUB-PARTE de otra y\n"
    "debería colgar de ella («pertenece a»).\n\n"
    "Casos de pertenencia (ejemplos):\n"
    "- un PROGRAMA/carrera/facultad/escuela pertenece a su UNIVERSIDAD\n"
    "  (ej. 'Ingeniería Mecánica - Universidad del Norte' pertenece a 'Universidad del Norte');\n"
    "- un PRODUCTO/marca pertenece a su EMPRESA (ej. 'Steam' pertenece a 'Valve Corporation');\n"
    "- una FILIAL pertenece a su MATRIZ; un ÁREA/equipo pertenece a su organización.\n\n"
    "Reglas estrictas:\n"
    "- `child_id`: el `id` EXACTO de la organización sub (de la lista). Cada `child_id` UNA vez.\n"
    "- El padre se indica de UNA de dos formas (exactamente una, nunca ambas):\n"
    "  • `parent_id`: el `id` de la organización padre, SI está en la lista; o\n"
    "  • `parent_name`: el nombre del padre cuando DEBERÍA existir pero NO está en la lista\n"
    "    (ej. el nombre del sub trae la universidad pero esa universidad no figura como entrada).\n"
    "- `cleaned_name` (opcional): el nombre del sub SIN el padre, si el nombre los junta\n"
    "  (ej. 'Ingeniería Mecánica - Universidad del Norte' → 'Ingeniería Mecánica').\n"
    "- SESGO A PRECISIÓN: incluí una entrada SOLO si estás seguro de la pertenencia. Ante la\n"
    "  duda, NO la incluyas (mejor que quede sin padre a inventar una jerarquía falsa).\n"
    "- NO relaciones organizaciones que son PARES o del mismo rubro; solo sub→contenedora.\n"
    "- NO inventes pertenencias para llenar; muchas orgs no tienen padre y eso está bien.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"links": [{"child_id": <id>, "parent_id": <id|null>, "parent_name": "<nombre|null>", '
    '"cleaned_name": "<nombre|null>"}]}\n'
    'Si no hay ninguna pertenencia clara, devolvé {"links": []}.'
)
