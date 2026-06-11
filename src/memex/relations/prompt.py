"""Prompts LLM del grafo: el PARTIDOR de cúmulos (`clusters_llm`) y el RESOLVER par-por-par
(`resolve_llm`).

Partidor: el LLM recibe DOS bloques de UN blob detectado: los VÉRTICES miembros y las ARISTAS entre
ellos (tipo, productor, nivel, evidencia). Devuelve una PARTICIÓN: los N contextos coherentes que el
blob contenga (0, 1 o varios), cada uno nombrado/descrito por lo que realmente hay. El SENTIDO
EMERGE de las entidades y conexiones — NO se siembran categorías ni ejemplos con nombres reales que
sesguen.

Resolver: el LLM recibe UN mensaje real + los pares de entidades que co-ocurrieron en él, y decide
por PAR si el mensaje evidencia una relación real (con cita textual OBLIGATORIA, verificada
determinista por el grounder) o una co-aparición casual. Las señales deterministas (encabezados de
correo masivo) van como contexto NEUTRO, nunca como veredicto sembrado; el resumen previo del
summarizer (si existe) va como contexto DERIVADO, nunca citable.
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

# --- RESOLVER par-por-par (zona gris): un mensaje + sus pares de co-ocurrencia ---------- #
GRAPH_RESOLVE_SYSTEM_PROMPT = (
    "Sos un analista de un grafo de conocimiento personal. Te paso UN mensaje real (correo, chat "
    "o post) y una lista numerada de PARES de entidades que aparecieron juntas en ese mensaje. "
    "Para CADA par, decidí si ESTE mensaje evidencia una relación REAL entre las dos entidades o "
    "solo una co-aparición casual.\n\n"
    "Veredictos posibles:\n"
    "- confirm: el mensaje evidencia un vínculo real (una compra/factura de ese producto o "
    "servicio, personas coordinando algo juntas, una entidad que es parte de la otra...). "
    "OBLIGATORIO acompañarlo de `quote`: el fragmento TEXTUAL del mensaje, copiado tal cual, que "
    "lo demuestra. Un confirm sin cita textual verificable se descarta.\n"
    "- reject: el mensaje muestra que es co-aparición sin vínculo (un listado promocional, un "
    "digest de noticias, menciones inconexas en el mismo texto).\n"
    "- dejar: este mensaje no alcanza para decidir. Ante la duda, dejar.\n\n"
    "Juzgá SOLO lo que este mensaje muestra: no uses conocimiento externo sobre las entidades. "
    "Si el mensaje trae una nota de señales deterministas (p.ej. encabezados de correo masivo), "
    "es CONTEXTO, no un veredicto: un correo masivo igual puede contener una relación real.\n\n"
    "A veces va un bloque RESUMEN PREVIO: un resumen DERIVADO que se generó antes (puede cubrir "
    "un lote de varios mensajes). Usalo solo para orientar la lectura; NO es evidencia. La "
    "`quote` de un confirm se copia del MENSAJE, nunca del resumen (una cita del resumen se "
    "descarta). Tampoco rechaces solo por lo que dice el resumen: si el MENSAJE no alcanza "
    "para decidir, es dejar.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"verdicts": [{"pair": <id del par>, "verdict": "confirm|reject|dejar", '
    '"quote": "<fragmento textual o vacío>", "confidence": <0..1>}, ...]}'
)
