"""Prompts + parsers del gate de relevancia y de la minería de reglas.

El criterio del portero: el blacklist determinista ya filtró newsletters obvias; lo que llega
acá es batch/individual. `relevant` = contenido con valor para el archivo personal (hechos de
la vida del usuario: transacciones, eventos, trámites, comunicaciones dirigidas) O publicidad
que toca un INTERÉS declarado (los intereses son la lista de rescate: la motivación del módulo
es que el router descartaba promos de Steam que el dueño SÍ quiere). `not_relevant` =
publicidad/ruido genérico que no toca ningún interés. Ante duda → `insufficient` (cola de
revisión manual): el gate nunca adivina.

Parsers tolerantes (precedente `parse_routing`): JSON inválido → None (la ventana queda en
error, reintentable); un id sin veredicto o con veredicto inválido cae a `insufficient`
(fallback conservador → lo decide el humano, no se pierde ni se procesa a ciegas).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from memex.relevance.verdicts import VERDICTS

GATE_SYSTEM_PROMPT = (
    "Sos el PORTERO de relevancia de un archivo personal: decidís qué correos vale la pena "
    "procesar (resumir y extraer datos) y cuáles son ruido. Te paso los INTERESES PERSONALES "
    "del usuario y una lista de correos en JSON (cada uno con `id`, `ts` y `text`).\n"
    "Veredicto por correo:\n"
    "- `relevant`: tiene valor para el archivo personal — hechos REALES de la vida del usuario "
    "(transacciones/recibos con monto, COBROS/deudas/avisos de cobranza —aunque no traigan "
    "monto exacto, ej. 'reporte a centrales de riesgo'—, eventos a los que asiste, trámites, "
    "viajes, mensajes de PERSONAS dirigidos a él) — O una OPORTUNIDAD CONCRETA que encaje con "
    "un interés (empleo, beca, evento, hackathon) o una novedad/lanzamiento de una herramienta "
    "que el usuario USA. "
    "La publicidad NO es relevant por mencionar un tema de pasada: solo si es una oferta concreta "
    "sobre algo que usa o le sirve de verdad. Un CONCURSO/competencia que es gancho de marketing "
    "de una herramienta (concursos de copywriting/cold-email, 'poné tu nombre frente a N "
    "personas') es promo, NO una oportunidad real: va a `not_relevant`. Las oportunidades reales "
    "(hackathon, beca, monitoría, convocatoria de su U u orgs reales) siguen relevant. "
    "UMBRAL DE SIGNIFICANCIA: lo MENOR, trivial o muy "
    "pequeño NO es relevant aunque toque un interés o una herramienta que usa — un cambio de "
    "versión sin impacto, una mejora menor, una novedad de IA muy simple o un anuncio chico van a "
    "`not_relevant`. Solo lo SUSTANTIVO o de impacto real entra; ante algo poco relevante o muy "
    "pequeño, preferí `not_relevant`.\n"
    "- `not_relevant`: publicidad/promoción genérica que no encaja con un interés concreto; los "
    "reportes/digests automáticos de estadísticas de una herramienta (ej. WakaTime weekly/yearly "
    "de tiempo de código) —el dato vive en la herramienta, el email es solo un digest—; los "
    "CHANGELOGS-lista, newsletters y webinars de una herramienta que usa (solo un RELEASE MAYOR "
    "con nombre —ej. 'Notion 3.3', 'Gemini 3.1', 'Claude Opus 4.6'— es relevant; el "
    "changelog/newsletter/webinar general NO); las notificaciones RUTINARIAS de plataformas de "
    "deploy (Railway/Vercel: 'deployment crashed', 'build failed', 'usage alert') son ruido de "
    "desarrollo (pasan todo el tiempo), NO incidentes archivables —pero las alertas de SEGURIDAD "
    "de sus repos (secrets/credentials expuestos) SÍ entran—; Y las "
    "notificaciones automáticas RUTINARIAS de seguridad o cuenta (alertas de login, 2FA, OAuth, "
    "passkey, 'verificá tu email', 'almacenamiento lleno', cambio de contraseña) — son ruido "
    "operativo, NO hechos archivables, salvo que reporten una transacción real o exijan una acción "
    "concreta del usuario. También es `not_relevant` el marketing de cursos/certificados pagos "
    "(Coursera/IBM/Cisco: 'professional certificate', sales/ofertas) y los CURSOS/charlas/eventos "
    "para APRENDER a usar IA aplicada (prompt engineering, 'aprende a usar Claude/Cursor', 'GenAI "
    "skills para el CV', 'AI Dev'/'build your AI skills'): el usuario quiere IA/ML técnica "
    "de fondo (transformers, entrenamiento, modelos matemáticos, papers), NO aprender el "
    "nivel aplicado. IMPORTANTE: SÍ son `relevant` las NOVEDADES o FEATURES de IA de "
    "herramientas que el usuario USA (ej. JetBrains coding-agent/Junie, Notion Agent, "
    "features de IA de OpenAI/GitHub): es novedad de SU herramienta y la quiere saber; lo "
    "ÚNICO de IA aplicada que es ruido son los CURSOS para "
    "aprender a usarla. PERO "
    "las actividades de SEMILLEROS y de la propia universidad del usuario (charlas, talleres, "
    "convocatorias) SÍ entran aunque el tema sea IA aplicada — son vida universitaria, no "
    "marketing externo. También es "
    "`not_relevant` la vida pastoral/religiosa genérica de la universidad (Cuaresma, Semana Santa, "
    "Pascua, Ejercicios Espirituales, misas, retiros espirituales), SALVO que sea un "
    "voluntariado o servicio social/comunitario concreto (tipo Misión País). Los retiros y "
    "ejercicios espirituales en sí NO entran. También es `not_relevant` el marketing de "
    "POSGRADOS/maestrías de la propia universidad ('inscríbete a tu posgrado', 'continúa tus "
    "estudios con nosotros', masterclasses de maestría): el usuario es de pregrado y por ahora no "
    "le interesan.\n"
    "- `insufficient`: no se puede decidir con este contenido (ambiguo, cortado, sin señal). "
    "Ante la duda usá `insufficient`, NUNCA adivines.\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"verdicts": [{"id": <id del correo>, "verdict": "relevant" | "not_relevant" | '
    '"insufficient", "reason": "<motivo corto, max 120 chars>"}, ...]}\n'
    "Incluí un veredicto para CADA correo de la lista, sin texto fuera del JSON."
)


def build_gate_user_content(interests: Sequence[str], messages_json: str) -> str:
    """Arma el turno `user` del gate: intereses (bullets) + mensajes (misma convención de
    marcador `Mensajes (JSON):` que el ruteo, para que los fakes de test ramifiquen igual)."""
    interests_str = (
        "\n".join(f"- {i}" for i in interests) if interests else "- (sin intereses declarados)"
    )
    return (
        f"Intereses personales del usuario:\n{interests_str}\n\nMensajes (JSON):\n{messages_json}"
    )


def _strip_fences(content: str) -> str:
    """Tolera respuestas envueltas en fences ```json ... ``` (los modelos a veces los agregan)."""
    cleaned = content.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1 :]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    return cleaned.strip()


#: reason de fallback cuando el LLM omitió o malformó el veredicto de un id esperado.
_FALLBACK_REASON = "veredicto faltante o inválido del LLM"


def parse_gate_verdicts(content: str, expected_ids: set[int]) -> dict[int, tuple[str, str]] | None:
    """Parsea `{"verdicts": [...]}` → {inbox_id: (verdict, reason)} cubriendo TODOS los ids.

    JSON inválido o shape inesperado → None (ventana en error, reintentable). Un id esperado
    sin veredicto válido cae a `insufficient` (conservador). Ids no esperados se ignoran.
    """
    try:
        data = json.loads(_strip_fences(content))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("verdicts"), list):
        return None

    raw_by_id: dict[int, tuple[str, str]] = {}
    for item in data["verdicts"]:
        if not isinstance(item, dict):
            continue
        try:
            iid = int(item.get("id"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        verdict = str(item.get("verdict", "")).strip()
        if verdict not in VERDICTS:
            continue
        raw_by_id[iid] = (verdict, str(item.get("reason", "")).strip())

    return {iid: raw_by_id.get(iid, ("insufficient", _FALLBACK_REASON)) for iid in expected_ids}


#: Espec común del formato de regla COMPUESTA (remitente + asunto, los dos obligatorios) + salida.
_COMPOSITE_RULE_SPEC = (
    "Cada regla DEBE combinar un REMITENTE y un PATRÓN DE ASUNTO (los dos, con AND):\n"
    "- `sender_kind`: 'sender_email' (remitente exacto) | 'sender_domain' (dominio exacto) | "
    "'list_id' (List-Id exacto); `sender_value`: el valor.\n"
    "- `subject_pattern`: substring del asunto que delimita ESA clase de correos (ej. 'oferta', "
    "'práctica profesional').\n"
    "El remitente solo es demasiado grueso: el patrón del asunto acota el subconjunto. Si para un "
    "remitente NO ves un patrón de asunto claro y recurrente, NO propongas regla (datos "
    "insuficientes para esa clase).\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"rules": [{"sender_kind": "<kind>", "sender_value": "<valor>", "subject_pattern": '
    '"<substring del asunto>", "rationale": "<por qué, max 200 chars>"}, ...]}\n'
    'Si no hay patrones claros, devolvé {"rules": []}.'
)

BLOCK_RULES_SYSTEM_PROMPT = (
    "Sos el analista de patrones de un gate de relevancia de correos. Te paso un AGREGADO de los "
    "correos que el gate marcó como NO relevantes (publicidad/ruido), agrupados por dominio del "
    "remitente, con conteos, remitentes y asuntos de ejemplo. Proponé reglas DETERMINISTAS para "
    "que esa clase de correos NO vuelva a gastar LLM (quedan `not_relevant`).\n"
    + _COMPOSITE_RULE_SPEC
    + "\nCada regla se validará contra el histórico (dry run): si atrapa un correo RELEVANTE será "
    "rechazada, así que sé preciso, no agresivo."
)

ALLOW_RULES_SYSTEM_PROMPT = (
    "Sos el analista de patrones de un gate de relevancia de correos. Te paso un AGREGADO de los "
    "correos que el gate marcó como RELEVANTES (con valor para el archivo personal) o que el dueño "
    "rescató a mano, agrupados por dominio del remitente, con conteos, remitentes y asuntos de "
    "ejemplo. Proponé reglas DETERMINISTAS para que esa clase de correos ENTRE directo sin gastar "
    "LLM (quedan `relevant`).\n"
    + _COMPOSITE_RULE_SPEC
    + "\nCada regla se validará contra el histórico (dry run): si atrapa un correo NO relevante "
    "será rechazada, así que sé preciso, no agresivo."
)


def rules_system_prompt(effect: str) -> str:
    """El system prompt de minería según la polaridad (`allow`=relevantes; el resto, ruido)."""
    return ALLOW_RULES_SYSTEM_PROMPT if effect == "allow" else BLOCK_RULES_SYSTEM_PROMPT


def build_rules_user_content(aggregates_json: str) -> str:
    """Arma el turno `user` de la minería: el agregado por remitente en JSON."""
    return f"Correos agrupados por remitente (JSON):\n{aggregates_json}"


def parse_rule_proposals(content: str) -> list[dict[str, str]] | None:
    """Parsea `{"rules": [...]}` → [{sender_kind, sender_value, subject_pattern, rationale}].

    None si el JSON es inválido. Las reglas mineadas son COMPUESTAS: una propuesta sin remitente
    válido (kind+value) Y sin patrón de asunto se descarta (no rompe la corrida) — incluye el caso
    «datos insuficientes» (el LLM devuelve `{"rules": []}`). La validación REAL es el dry run del
    caller; acá solo se sanea el shape.
    """
    from memex.relevance.rules import SENDER_KINDS

    try:
        data = json.loads(_strip_fences(content))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        return None
    proposals: list[dict[str, str]] = []
    for item in data["rules"]:
        if not isinstance(item, dict):
            continue
        sender_kind = str(item.get("sender_kind", "")).strip()
        sender_value = str(item.get("sender_value", "")).strip()
        subject_pattern = str(item.get("subject_pattern", "")).strip()
        if sender_kind not in SENDER_KINDS or not sender_value or not subject_pattern:
            continue
        proposals.append(
            {
                "sender_kind": sender_kind,
                "sender_value": sender_value,
                "subject_pattern": subject_pattern,
                "rationale": str(item.get("rationale", "")).strip(),
            }
        )
    return proposals


def build_messages_json(rows: Sequence[Any], rendered: Sequence[str]) -> str:
    """JSON `[{id, ts, text}]` del lote (misma forma que el ruteo del orquestador)."""
    items = [
        {"id": row.inbox_id, "ts": row.occurred_at.isoformat(), "text": text}
        for row, text in zip(rows, rendered, strict=True)
    ]
    return json.dumps(items, ensure_ascii=False)
