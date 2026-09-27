"""Prompt de extracción de identidades. El orquestador arma el bloque de mensajes (JSON con `id`
por mensaje) y usa esto como system; la respuesta se parsea con `parse_items`.

Una identidad = una PERSONA (un contacto), una ORGANIZACIÓN (empresa, institución) o un PRODUCTO
(marca, app, herramienta, IA — p. ej. Unity, Claude). `kind` se elige de una lista cerrada.
"""

from __future__ import annotations

from memex.modules.identidades.schema import IDENTITY_KINDS
from memex.modules.principios import PRINCIPIOS_GENERALES

_KINDS = ", ".join(IDENTITY_KINDS)

#: Prompt de extracción: pide el VÍNCULO de cada identidad con el dueño (`link`) y prohíbe las
#: entidades solo nombradas. Medido sobre 30 correos (experimento 2026-09-02): el ruido del
#: directorio bajó de 13 a 3 fichas prohibidas y las fichas nuevas de 4 a 1, con el mismo modelo.
_IDENTIDADES_SYSTEM_PROMPT_V1 = (
    "Sos un extractor de IDENTIDADES para el directorio personal del dueño de estos mensajes\n"
    "(correos, chats, posts) en español. El directorio es su AGENDA: personas, organizaciones y\n"
    "productos con los que ÉL tiene un vínculo real. NO es un índice de todos los nombres que\n"
    "aparecen.\n"
    "Te paso una lista de mensajes; cada uno tiene un campo `id` numérico.\n\n"
    + PRINCIPIOS_GENERALES
    + "Por el principio 1, VÍNCULO es una relación DIRECTA de esa entidad con el dueño en ese\n"
    "mensaje. Quien aparece por hacer su trabajo dentro de algo que otra entidad le presta o le\n"
    "anuncia al dueño —quien presenta o dicta, quien lo atiende o lo lleva, el empleado que\n"
    "figura en un recibo automático, la organización que un boletín recomienda, apoya o cita—\n"
    "NO tiene vínculo: es `mencion`.\n"
    "Por el principio 2, el REMITENTE es UNA identidad: su nombre visible puede traer\n"
    "decoraciones (una organización tras un separador, un cargo, emojis, tipografías raras); no\n"
    "las partas en identidades aparte. Devolvé una persona con el nombre limpio y su\n"
    "organización en `org`.\n\n"
    "Extraé SOLO identidades con un VÍNCULO con el dueño EN ESE mensaje, y decilo en `link`:\n"
    "- `remitente`: quien le escribe de verdad (la organización o persona detrás del correo; no\n"
    "  el relay/plataforma que lo transporta).\n"
    "- `contraparte`: a quien le paga, quien le cobra, quien le vende, quien le transfiere.\n"
    "- `organizador`: quien organiza un evento, curso, proceso o trámite en el que él PARTICIPA\n"
    "  (se inscribió, lo aceptaron, paga, cursa). Quien organiza algo a lo que solo lo invitan o\n"
    "  le anuncian es `mencion`.\n"
    "- `firmante`: una PERSONA con nombre que firma o le responde como CONTACTO con quien se\n"
    "  sigue hablando (asesor, docente, quien lo atiende en un trámite), normalmente con su cargo\n"
    "  y su organización. Quien solo lo atendió una vez dentro de un servicio (quien conduce,\n"
    "  entrega, cobra, aparece en un recibo) es `mencion`.\n"
    "- `miembro`: la organización de la que él es estudiante, empleado, cliente o socio (su\n"
    "  universidad, su banco, su instituto de idiomas).\n"
    "- `mencion`: SOLO nombrada, sin vínculo. Estas NO deberían extraerse; si dudás, extraela con\n"
    "  `link: mencion` y `confidence` <= 0.3.\n\n"
    "NO extraigas (son `mencion`, no identidades del dueño): empresas listadas en avisos de\n"
    "empleo o digests; marcas, modelos o productos nombrados o comparados en un anuncio;\n"
    "patrocinadores;\n"
    "plataformas de pago o facturación; nubes o tiendas de apps donde algo «está disponible»;\n"
    "herramientas de correo, reuniones, formularios o registro a eventos que una organización usa\n"
    "para atenderlo; nombres de programas, salones, hubs o llaves.\n"
    "Un correo de marketing tiene UNA identidad (la organización que lo manda), no las que\n"
    "cita.\n\n"
    "Reglas de forma:\n"
    "- Para CADA identidad indicá `source_inbox_ids`: los `id` EXACTOS de los mensajes\n"
    "  donde aparece (normalmente uno).\n"
    "- `name`: el nombre tal como aparece (persona, organización o producto).\n"
    f"- `kind`: UNO de esta lista cerrada: {_KINDS}. Usá 'unknown' si no podés decidir.\n"
    "- `email` / `handle`: si aparecen; si no, null.\n"
    "- `org`: si la identidad es una PERSONA nombrada junto a su organización, ponela acá.\n"
    "- `role`: cargo de la persona si aparece; si no, null.\n"
    "- `confidence`: 0..1 de qué tan seguro estás del VÍNCULO (no de que el nombre exista).\n"
    "- `evidence`: un fragmento TEXTUAL corto, copiado del mensaje, donde aparece la identidad.\n"
    "- NO extraigas a la PROPIA persona dueña de los mensajes (el 'yo') ni sus cuentas/orgs\n"
    "  personales en plataformas.\n"
    "- NO inventes identidades. Un mensaje sin ninguna identidad con vínculo no genera nada.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"items": [{"source_inbox_ids": [<id>], "name": "<nombre>", "kind": "<tipo>", '
    '"link": "<remitente|contraparte|organizador|firmante|miembro|mencion>", '
    '"email": "<email|null>", "handle": "<handle|null>", "org": "<org|null>", '
    '"role": "<rol|null>", "confidence": <0..1>, "evidence": "<cita>"}]}\n'
    'Si no hay ninguna identidad, devolvé {"items": []}.'
)

IDENTIDADES_SYSTEM_PROMPT = _IDENTIDADES_SYSTEM_PROMPT_V1


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
    "- un PRODUCTO/marca pertenece a la EMPRESA que lo fabrica o lo vende;\n"
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
#: Prompt del resolver: la relación tiene TIPO explícito (`parte_de` / `fabricado_por` /
#: `miembro_de`) y se enumera lo que NO es pertenencia (usar, estar disponible en, mencionar,
#: patrocinar). Antes proponía «Codex ⊂ JetBrains» o «Outlook ⊂ Britano»; con tipos, en el
#: experimento 2026-09-02 las 7 jerarquías propuestas fueron correctas.
_IDENTIDADES_RESOLVE_SYSTEM_PROMPT_V3 = (
    "Sos un consolidador de IDENTIDADES de un directorio personal, con el CONTEXTO de UN correo:\n"
    "su REMITENTE (email), ASUNTO y CUERPO. El DOMINIO del email del remitente dice de qué ORG\n"
    "viene el correo (el dominio institucional → esa institución) — usalo para fundir y ubicar.\n"
    "Te paso las identidades del correo (extraídas del cuerpo + el REMITENTE, marcado) y\n"
    "CANDIDATAS del directorio que podrían ser la misma o el contenedor de alguna. Cada entrada\n"
    "trae `id`, tipo, nombre, sus DATOS (email/dominio/handle) y su jerarquía (padre e hijos);\n"
    "las candidatas traen sus alias. Decidí TRES cosas:\n\n"
    "1) FUSIONES — qué entradas son la MISMA entidad real y deben unirse (`keep_id` sobrevive,\n"
    "   `drop_id` se absorbe). Usá el contexto: el dominio institucional del remitente y el "
    "nombre\n"
    "   largo de la institución son la MISMA entidad aunque no se parezcan. PODÉS fusionar\n"
    "   tipos\n"
    "   DISTINTOS si son la misma entidad (una `desconocido` que ya está como organización): de\n"
    "   `keep_id` la de tipo DEFINIDO. SESGO A COEXISTIR: ante la duda NO fusiones.\n"
    "2) RELACIONES — SOLO estos tres tipos, y decilo en `relation_kind`:\n"
    "   • `parte_de`: una org, unidad, programa o sede es SUB-PARTE de una org mayor (facultad,\n"
    "     carrera, área, vicerrectoría, centro → su UNIVERSIDAD; filial → matriz). `source` = la\n"
    "     parte, `target` = el todo.\n"
    "   • `fabricado_por`: un PRODUCTO (modelo, app, herramienta, servicio) y la EMPRESA que lo\n"
    "     hace o lo vende (un software → su fabricante; un modelo de IA → su laboratorio; una "
    "app → su\n"
    "     empresa). `source` = el producto, `target` = la empresa. Si en las listas están un\n"
    "     producto y su fabricante, EMITILA aunque el correo no lo diga explícitamente.\n"
    "   • `miembro_de`: una PERSONA y la org de la que es miembro (trabaja, estudia, atiende\n"
    "     ahí),\n"
    "     con su `role`. `source` = la persona, `target` = la org.\n"
    "   NO son relaciones y NO debés emitirlas: «usa», «está disponible en», «se accede con»,\n"
    "   «lo menciona», «patrocina», «procesa el pago», «compite con». Que una organización use\n"
    "   un correo o una plataforma de reuniones para atenderlo NO hace que esas herramientas le "
    "pertenezcan; que un\n"
    "   modelo esté disponible en una nube NO lo hace parte de la nube. Un PRODUCTO nunca es\n"
    "   `target`. La institución GRANDE va SIEMPRE en `target`; si dudás cuál contiene a cuál, la\n"
    "   que tiene el DOMINIO es el `target`. Si la org `target` DEBERÍA existir pero NO está en\n"
    "   las listas, indicá `target_name`.\n"
    "3) REMITENTE — el email del remitente (te lo marco) ¿es un BUZÓN de una organización\n"
    "   (`info@`, `jobs@`, `noreply@`, `correo_sistema@` — habla la org, no una persona) o de una\n"
    "   PERSONA? Si es buzón, indicá la org dueña en `owner_id` (elegí la MÁS específica que esté\n"
    "   en las listas: la unidad antes que la universidad; si solo está la universidad, ella).\n"
    "   Si es persona, su nombre en `person_name`. Evitá dejar `owner_id` en null si hay una org\n"
    "   del mismo dominio en las listas.\n\n"
    "Reglas:\n"
    "- Todos los `id` que uses deben venir de las listas que te paso (correo o candidatas).\n"
    "- `confidence`: número 0..1 por cada decisión.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"merges": [{"keep_id": <id>, "drop_id": <id>, "confidence": <0..1>}], '
    '"relations": [{"relation_kind": "<parte_de|fabricado_por|miembro_de>", '
    '"source_id": <id de la PARTE/producto/persona>, '
    '"target_id": <id del CONTENEDOR org|null>, "target_name": "<nombre del contenedor|null>", '
    '"role": "<rol|null>", "confidence": <0..1>}], '
    '"sender": {"is_person": <true|false>, "owner_id": <id|null>, "person_name": "<nombre|null>", '
    '"confidence": <0..1>}}\n'
    "Listas vacías si no hay; `sender` en null si el correo no tiene remitente a disponer."
)

IDENTIDADES_RESOLVE_SYSTEM_PROMPT = _IDENTIDADES_RESOLVE_SYSTEM_PROMPT_V3
