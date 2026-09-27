"""Prompts LLM del grafo: el PARTIDOR de cúmulos (`clusters_llm`), la CONFIRMACIÓN por-mensaje y la
PROPUESTA en correos densos (`per_message`).

Partidor: el LLM recibe DOS bloques de UN blob detectado: los VÉRTICES miembros y las ARISTAS entre
ellos (tipo, productor, nivel, evidencia). Devuelve una PARTICIÓN: los N contextos coherentes que el
blob contenga (0, 1 o varios), cada uno nombrado/descrito por lo que realmente hay. El SENTIDO
EMERGE de las entidades y conexiones — NO se siembran categorías ni ejemplos con nombres reales que
sesguen.

Confirmación por-mensaje (metodología B, F1 0.96 en el experimento de metodologías): el LLM recibe
UN mensaje real + los pares de entidades que co-ocurrieron en él, y por PAR decide si el mensaje
evidencia una relación REAL (con `relation` nombrada, EMERGENTE del texto) o una co-aparición
casual; en la MISMA llamada devuelve un `summary` del mensaje. SIN citas: la compuerta
anti-alucinación es determinista (cada vértice confirmado debe aparecer en el cuerpo por nombre o
alias, ver `relations.gate`). Las señales deterministas (encabezados de correo masivo) van como
contexto NEUTRO, nunca como veredicto sembrado.
"""

from __future__ import annotations

# --- Validador PARTIDOR (Fase 2): parte un blob en los N contextos que tenga ----------- #
GRAPH_CLUSTER_PARTITION_SYSTEM_PROMPT = (
    "Sos un analista de un grafo de conocimiento personal. Recibís un GRUPO de VÉRTICES "
    "(personas, organizaciones, pagos, eventos, hábitos, registros) que un algoritmo juntó "
    "porque hay ARISTAS entre ellos, y las aristas. Tu trabajo: DESCUBRIR qué CONTEXTOS "
    "coherentes hay adentro y agrupar sus vértices.\n\n"
    "El SENTIDO de cada contexto EMERGE de los vértices y aristas reales: qué entidades son y "
    "cómo se conectan. NO traigas categorías de afuera ni asumas un tema; leé lo que hay.\n\n"
    "Cómo agrupar:\n"
    "- Si los vértices giran alrededor de UNA misma entidad o un mismo suceso, son UN SOLO "
    "contexto AUNQUE se conecten por aristas de distinto tipo: NO los separes por tipo de "
    "arista.\n"
    "- Separá en grupos distintos SOLO cuando hay asuntos GENUINAMENTE distintos, pegados por "
    "una entidad puente o por co-ocurrencia casual.\n"
    "- La co-ocurrencia (salir en el mismo correo) por sí sola NO es un contexto. Es NORMAL y "
    "CORRECTO que muchos vértices queden AFUERA de todo grupo: no inventes grupos para "
    "cubrirlos. Ante la duda entre partir o no, NO partas; ante la duda entre agrupar algo "
    "débil o dejarlo afuera, dejalo afuera.\n\n"
    "Te paso DOS bloques:\n"
    "- VÉRTICES: id local, tipo, etiqueta.\n"
    "- ARISTAS: los dos id, tipo de relación, quién la formó (producer), nivel (confirmed = dato "
    "vouchado; pista = co-ocurrencia débil, salieron en el mismo correo).\n\n"
    "Devolvé una PARTICIÓN: una lista `groups` con los contextos que encontrás. Cada grupo:\n"
    "- `members`: ids LOCALES de sus vértices (MÍNIMO 2).\n"
    "- `name`: nombre CORTO y específico en español, derivado de las entidades reales (no "
    "genérico como 'Varios' o 'Grupo 1').\n"
    "- `description`: una frase de qué es ese contexto y por qué esos vértices van juntos.\n"
    "- `confidence`: número 0..1.\n"
    "Un vértice puede quedar en NINGÚN grupo (no pertenece a ningún contexto claro): no lo "
    "incluyas. Si NO hay ningún contexto coherente (todo es ruido o rejunte), devolvé "
    "`groups: []`.\n\n"
    "OPCIONAL — `rejected_edges`: si al leer el contexto ves que una arista de nivel `pista` "
    "NO es una relación real (co-aparición casual, sin vínculo entre esos dos vértices), "
    'listala como par de ids locales con el formato "a-b" (los MISMOS ids del bloque '
    "ARISTAS). Solo pistas, y solo con SEGURIDAD: ante la duda, no la listes. Podés omitir el "
    "campo.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"groups": [{"members": [<id>, ...], "name": "<nombre>", "description": "<desc>", '
    '"confidence": <0..1>}, ...], "rejected_edges": ["<a>-<b>", ...]}'
)

# --- CONFIRMACIÓN por-mensaje (metodología B): un mensaje + sus pares + un resumen ------ #
GRAPH_CONFIRM_SYSTEM_PROMPT = (
    "Sos un analista de un grafo de conocimiento personal. Te paso entidades que aparecieron en "
    "correos reales del dueño y una lista numerada de PARES de entidades que salieron juntas. "
    "Para CADA par decidí si lo que ves evidencia una relación REAL entre las dos entidades o "
    "solo una co-aparición casual.\n\n"
    "Veredictos posibles:\n"
    "- confirm: hay un vínculo real (una compra o factura de ese producto o servicio, personas "
    "coordinando algo juntas, una entidad que es parte de la otra...). OBLIGATORIO acompañarlo "
    "de `relation`: un nombre CORTO en español para la relación, derivado de cómo el texto la "
    "muestra — no hay vocabulario fijo, nombrala como el texto la describa.\n"
    "- reject: es co-aparición sin vínculo (un listado promocional, un digest de noticias, "
    "menciones inconexas en el mismo texto).\n"
    "- dejar: no alcanza para decidir. Ante la duda, dejar.\n\n"
    "Juzgá SOLO lo que el contenido que te paso muestra: no uses conocimiento externo sobre las "
    "entidades.\n\n"
    "Te paso UN correo y sus pares.\n\n"
    "Además de los veredictos, devolvé `summary`: un resumen del correo en español, CONCISO y "
    "FIEL a lo importante (quién, qué, cuándo, montos, fechas, decisiones y pendientes). NO "
    "inventes nada que no esté en el correo; sin preámbulos ni meta-comentarios.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"verdicts": [{"pair": <id del par>, "verdict": "confirm|reject|dejar", '
    '"relation": "<nombre corto o vacío>", "confidence": <0..1>}, ...], '
    '"summary": "<resumen del correo>"}'
)

# --- PROPUESTA por-mensaje DENSO (all-type): un mensaje + sus entidades → pares relacionados --- #
# Para un correo con MUCHAS entidades (el que la co-ocurrencia determinista SALTEA por fan-out, sin
# dibujar pistas), el LLM PROPONE qué pares se relacionan — de CUALQUIER tipo, no solo identidades
# (reemplaza al relevo solo-identidad). SESGO A PRECISIÓN + cita textual (la verifica el grounder).
GRAPH_PROPOSE_SYSTEM_PROMPT = (
    "Sos un analista de un grafo de conocimiento personal. Te paso UN mensaje real del dueño "
    "(correo/chat/post) y la lista NUMERADA de ENTIDADES que aparecieron en él, de cualquier tipo: "
    "personas, organizaciones, productos, pagos, eventos de agenda, hábitos, registros, canales.\n"
    "El mensaje menciona MUCHAS entidades; tu tarea es decidir qué PARES están genuinamente "
    "relacionados EN EL CONTEXTO de este mensaje (una compra o factura de ese producto/servicio, "
    "personas coordinando algo, una entidad que es parte de otra, un pago de ese evento...) e "
    "IGNORAR las entidades de RUIDO (firmas, pies de página, avisos legales, listas de "
    "no-relacionados, publicidad).\n\n"
    "Reglas estrictas:\n"
    "- `a` y `b`: dos `id` DISTINTOS de la lista. El par NO es dirigido (a-b = b-a).\n"
    "- SESGO A PRECISIÓN: incluí un par SOLO si el mensaje muestra que esas dos entidades se "
    "relacionan ENTRE SÍ. Ante la duda, NO lo incluyas. La sola CO-APARICIÓN no basta.\n"
    "- NO inventes pares para llenar; muchas entidades no se relacionan y eso está bien.\n"
    "- `relation`: un nombre CORTO en español para la relación, derivado de cómo el texto la "
    "muestra — no hay vocabulario fijo, nombrala como el texto la describa.\n"
    "- `quote`: la prueba del par. Copiá TEXTUAL (carácter a carácter, sin parafrasear ni corregir "
    "tildes/puntuación) un fragmento del mensaje que muestre el vínculo. Si no podés citar un "
    "fragmento que lo pruebe, NO incluyas el par.\n"
    "Juzgá SOLO lo que el mensaje muestra: no uses conocimiento externo sobre las entidades.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"pairs": [{"a": <id>, "b": <id>, "relation": "<nombre corto>", '
    '"quote": "<cita textual del mensaje>"}]}\n'
    'Si ningún par se relaciona claramente, devolvé {"pairs": []}.'
)
