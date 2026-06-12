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
    "- `relevant`: tiene valor para el archivo personal — hechos de la vida del usuario "
    "(transacciones, recibos, eventos, trámites, viajes, comunicaciones dirigidas a él) — O es "
    "publicidad/oferta que toca alguno de sus intereses declarados.\n"
    "- `not_relevant`: publicidad, promoción o ruido genérico que NO toca ningún interés "
    "declarado y no aporta hechos personales.\n"
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


RULES_SYSTEM_PROMPT = (
    "Sos el analista de patrones de un gate de relevancia de correos. Te paso un AGREGADO de "
    "los correos que el gate marcó como NO relevantes (publicidad/ruido), agrupados por "
    "dominio del remitente, con conteos, remitentes y asuntos de ejemplo. Proponé reglas "
    "DETERMINISTAS para que esa clase de correos no vuelva a pasar por el LLM.\n"
    "Tipos de regla permitidos (`kind`):\n"
    "- `sender_email`: igualdad exacta del remitente (ej. promos@tienda.com)\n"
    "- `sender_domain`: igualdad exacta del dominio (ej. mailing.tienda.com) — preferilo solo "
    "si TODO lo de ese dominio es ruido\n"
    "- `subject_contains`: substring del asunto (ej. 'oferta exclusiva') — usalo para plantillas "
    "repetidas\n"
    "- `list_id`: igualdad exacta del List-Id\n"
    "Proponé SOLO reglas con apoyo claro en los datos (varios correos del mismo patrón). Cada "
    "regla se validará después contra el histórico (dry run): si atrapa un correo relevante "
    "será rechazada, así que sé preciso, no agresivo.\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"rules": [{"kind": "<kind>", "pattern": "<patrón>", "rationale": "<por qué, max 200 '
    'chars>"}, ...]}\n'
    'Si no hay patrones claros, devolvé {"rules": []}.'
)


def build_rules_user_content(aggregates_json: str) -> str:
    """Arma el turno `user` de la minería: el agregado de no-relevantes en JSON."""
    return f"Correos no relevantes agrupados (JSON):\n{aggregates_json}"


def parse_rule_proposals(content: str) -> list[dict[str, str]] | None:
    """Parsea `{"rules": [...]}` → [{kind, pattern, rationale}]. None si el JSON es inválido.

    Propuestas con kind desconocido o pattern vacío se descartan (no rompen la corrida). La
    validación REAL es el dry run del caller; acá solo se sanea el shape.
    """
    from memex.relevance.rules import RULE_KINDS

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
        kind = str(item.get("kind", "")).strip()
        pattern = str(item.get("pattern", "")).strip()
        if kind not in RULE_KINDS or not pattern:
            continue
        proposals.append(
            {"kind": kind, "pattern": pattern, "rationale": str(item.get("rationale", "")).strip()}
        )
    return proposals


def build_messages_json(rows: Sequence[Any], rendered: Sequence[str]) -> str:
    """JSON `[{id, ts, text}]` del lote (misma forma que el ruteo del orquestador)."""
    items = [
        {"id": row.inbox_id, "ts": row.occurred_at.isoformat(), "text": text}
        for row, text in zip(rows, rendered, strict=True)
    ]
    return json.dumps(items, ensure_ascii=False)
