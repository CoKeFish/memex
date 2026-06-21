"""Prompt de extracción de identidades. El orquestador arma el bloque de mensajes (JSON con `id`
por mensaje) y usa esto como system; la respuesta se parsea con `parse_items`.

Una identidad = una PERSONA (un contacto), una ORGANIZACIÓN (empresa, institución) o un PRODUCTO
(marca, app, herramienta, IA — p. ej. Unity, Claude). `kind` se elige de una lista cerrada.
"""

from __future__ import annotations

from memex.modules.identidades.schema import IDENTITY_KINDS

_KINDS = ", ".join(IDENTITY_KINDS)

IDENTIDADES_SYSTEM_PROMPT = (
    "Sos un extractor de IDENTIDADES mencionadas en mensajes personales (chats, correos, posts)\n"
    "en español. Una identidad es una PERSONA (un contacto, alguien con quien la persona\n"
    "interactúa), una ORGANIZACIÓN (empresa, institución, universidad — p. ej. Anthropic, Valve)\n"
    "o un PRODUCTO: marca, app, plataforma, herramienta o IA (p. ej. Unity, Claude, Steam).\n"
    "Te paso una lista de mensajes; cada uno tiene un campo `id` numérico.\n\n"
    "Reglas estrictas:\n"
    "- Para CADA identidad indicá `source_inbox_ids`: los `id` EXACTOS de los mensajes\n"
    "  donde aparece (normalmente uno).\n"
    "- `name`: el nombre tal como aparece (persona, organización o producto).\n"
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
    "Sos un desambiguador de IDENTIDADES (persona, organización o producto). Te paso dos\n"
    "entradas (A y B) de un directorio, cada una con tipo, nombre, alias e identificadores\n"
    "(emails, handles, dominios). Decidí si A y B son la MISMA identidad del mundo real.\n\n"
    "Reglas:\n"
    "- SESGO A COEXISTIR: ante la duda, NO son la misma. Solo decí que SÍ si hay evidencia clara\n"
    "  (mismo email/handle/dominio, o el mismo nombre con variantes obvias de la MISMA entidad).\n"
    "- Nombres parecidos pero de entidades distintas (homónimos, dos personas con igual nombre,\n"
    "  dos empresas distintas) → NO son la misma.\n"
    "- Identidades de TIPO distinto (persona/organización/producto) NUNCA son la misma.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"same": <true|false>, "confidence": <0..1>, "rationale": "<motivo breve>"}'
)


#: Organizador de PERTENENCIA («sub»): recibe la lista COMPLETA de organizaciones Y productos del
#: directorio (id interno + nombre + alias; los productos marcados `[producto]`) y arma la
#: jerarquía «pertenece a» (programa→universidad, producto→empresa, filial→matriz, área→org). UNA
#: sola llamada holística, SESGO A PRECISIÓN (ante la duda queda sin padre). El resultado se
#: aplica solo (sin confirmación manual).
IDENTIDADES_HIERARCHY_SYSTEM_PROMPT = (
    "Sos un organizador de la JERARQUÍA de un directorio de ORGANIZACIONES y PRODUCTOS. Te paso\n"
    "la lista completa, cada entrada con un `id` numérico, su nombre y sus alias; los PRODUCTOS\n"
    "van marcados con [producto]. Tu tarea es detectar relaciones de PERTENENCIA: cuando una\n"
    "entrada es una SUB-PARTE de otra y debería colgar de ella («pertenece a»).\n\n"
    "Casos de pertenencia (ejemplos):\n"
    "- un PROGRAMA/carrera/facultad/escuela pertenece a su UNIVERSIDAD\n"
    "  (ej. 'Ingeniería Mecánica - Universidad del Norte' pertenece a 'Universidad del Norte');\n"
    "- un PRODUCTO/marca pertenece a su EMPRESA (ej. 'Steam' pertenece a 'Valve Corporation');\n"
    "- una FILIAL pertenece a su MATRIZ; un ÁREA/equipo pertenece a su organización.\n\n"
    "Reglas estrictas:\n"
    "- `child_id`: el `id` EXACTO de la entrada sub (de la lista). Cada `child_id` UNA vez.\n"
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


#: Clasificador del TIPO de UNA entidad `desconocido`: persona / organización / producto, o
#: `desconocido` si la evidencia no alcanza. Le pasa nombre + identificadores (correo/dominio) +
#: afiliación de dominio + asuntos donde fue remitente. SESGO A NO ADIVINAR: ante la duda,
#: `desconocido` (queda pendiente, no se fuerza un tipo equivocado).
IDENTIDADES_CLASSIFY_SYSTEM_PROMPT = (
    "Sos un clasificador del TIPO de UNA entidad de un directorio personal, hoy sin tipo definido\n"
    "(`desconocido`). Te paso su nombre actual, sus identificadores (correo/dominio/handle), la\n"
    "organización del dominio a la que está afiliada (si la hay) y asuntos de correos donde fue\n"
    "el remitente. Decidí qué ES:\n\n"
    "- PERSONA: un individuo humano (un contacto, un nombre de persona; un correo personal).\n"
    "- ORGANIZACION: una empresa, institución o universidad — INCLUIDA una SUB-UNIDAD de una\n"
    "  (facultad, carrera, programa, vicerrectoría, decanatura, departamento, área, semillero,\n"
    "  oficina): una dependencia que habla por una organización es, ella misma, una organización.\n"
    "- PRODUCTO: una marca, app, plataforma, boletín o servicio automatizado.\n"
    "- DESCONOCIDO: si la evidencia no alcanza para decidir con seguridad.\n\n"
    "Reglas estrictas:\n"
    "- El DOMINIO del correo y los ASUNTOS son la señal más fuerte: un buzón de una dependencia\n"
    "  institucional → organización; un correo individual de una persona → persona.\n"
    "- Si el nombre actual es SOLO una dirección de correo (sin un nombre real de persona ni de\n"
    "  entidad), no se puede saber quién es → `desconocido`.\n"
    "- SESGO A NO ADIVINAR: ante la duda, devolvé `desconocido` (mejor pendiente que mal-tipar).\n"
    "- `confidence`: número 0..1 de qué tan seguro estás del tipo.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"kind": "<persona|organizacion|producto|desconocido>", "confidence": <0..1>, '
    '"rationale": "<motivo breve>"}'
)


#: Resolvedor CONTEXTUAL por-correo: con el asunto+cuerpo de UN correo decide, de una, tres cosas
#: sobre las identidades de ese correo (extraídas + remitente) y sus candidatas del directorio:
#: FUSIONES (la misma entidad, incluso si los nombres no se parecen — dominio↔nombre), JERARQUÍA
#: (sub→contenedora) y la DISPOSICIÓN DEL REMITENTE (buzón de una org vs persona). Reusa los sesgos
#: del dedup (coexistir) y del organizador (precisión).
IDENTIDADES_RESOLVE_SYSTEM_PROMPT = (
    "Sos un consolidador de IDENTIDADES de un directorio personal, con el CONTEXTO de UN\n"
    "correo (asunto + cuerpo). Te paso las identidades del correo (las extraídas del cuerpo +\n"
    "el REMITENTE, marcado) y CANDIDATAS del directorio que podrían ser la misma o el\n"
    "contenedor de alguna. Cada entrada trae `id`, tipo, nombre, sus DATOS (identificadores como\n"
    "email/dominio/handle — son atributos de la identidad) y su jerarquía (padre e hijos, si los\n"
    "hay); las candidatas traen sus alias. Con el contexto del correo decidí TRES cosas:\n\n"
    "1) FUSIONES — qué entradas son la MISMA entidad real y deben unirse (`keep_id`\n"
    "   sobrevive, `drop_id` se absorbe). Usá el contexto: p. ej. el dominio\n"
    "   `javeriana.edu.co` y `Pontificia Universidad Javeriana` son la MISMA universidad\n"
    "   aunque los nombres no se parezcan. PODÉS fusionar tipos DISTINTOS si son la misma\n"
    "   entidad (típico: una `desconocido` que ya está como organización) — poné de\n"
    "   `keep_id` la de tipo DEFINIDO, nunca la `desconocido`. SESGO A COEXISTIR: ante la\n"
    "   duda NO fusiones (no juntes una persona con una empresa por compartir nombre).\n"
    "2) RELACIONES DE PERTENENCIA — qué identidad (`source_id`) PERTENECE A / ES MIEMBRO DE\n"
    "   otra (`target_id`, SIEMPRE una organización). DOS casos, MISMA forma:\n"
    "   • una organización/producto que es SUB-PARTE de otra org (carrera/facultad→universidad,\n"
    "     producto→empresa, área→org); o\n"
    "   • una PERSONA que es MIEMBRO de una org (trabaja/estudia/preside…), con su `role`.\n"
    "   NO etiquetes el tipo: el sistema lo deduce por los tipos de source/target. `role` solo\n"
    "   si `source` es persona. Mapeá la org por CONTEXTO aunque el nombre no calce exacto\n"
    "   («Pontificia Universidad Javeriana» → la entrada cuyo dominio es javeriana.edu.co). Si la\n"
    "   org `target` DEBERÍA existir pero NO está en las listas, indicá `target_name`. Incluí las\n"
    "   relaciones razonables.\n"
    "3) REMITENTE — el email del remitente (te lo marco) ¿es un BUZÓN de una organización\n"
    "   (`info@`, `jobs@`, `contacto@` — habla la org, no una persona) o de una PERSONA? Si\n"
    "   es buzón, indicá la org dueña en `owner_id`. Si es persona, su nombre en `person_name`.\n\n"
    "Reglas:\n"
    "- Todos los `id` que uses deben venir de las listas que te paso (correo o candidatas).\n"
    "- `confidence`: número 0..1 por cada decisión.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"merges": [{"keep_id": <id>, "drop_id": <id>, "confidence": <0..1>}], '
    '"relations": [{"source_id": <id>, "target_id": <id|null>, "target_name": "<nombre|null>", '
    '"role": "<rol|null>", "confidence": <0..1>}], '
    '"sender": {"is_person": <true|false>, "owner_id": <id|null>, "person_name": "<nombre|null>", '
    '"confidence": <0..1>}}\n'
    "Listas vacías si no hay; `sender` en null si el correo no tiene remitente a disponer."
)
