"""Jueces atómicos de tareas: (1) «¿esta candidata es la MISMA tarea que la persona ya
completó, o una asignación nueva?» y (2) «¿es la MISMA tarea pendiente ya registrada, leída en
otra fuente con otras palabras, detalles nuevos o la fecha movida?». Arbitraje → codex
(consumer `tareas_judge`, fallback deepseek), como el resto de los jueces (regla del dueño:
DeepSeek para el volumen, codex para juzgar). Los llama la persistencia de `extract.py` solo
cuando la regla barata sospecha (`find_completed_cover`, `find_similar_candidates`); el título
exacto no pasa por acá. Prompts por categorías, sin marcas ni casos del usuario."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from memex.llm import ChatMessage, LLMClient, LLMResult

PURPOSE = "tareas_judge"
_MAX_TOKENS = 300
MIN_CONFIDENCE = 0.6

JUDGE_SYSTEM_PROMPT = (
    "Sos un juez de TAREAS personales. Te doy (A) una tarea que la persona ya marcó como\n"
    "HECHA, con su fecha de completado y el mensaje que la pidió, y (B) una candidata extraída\n"
    "de otro mensaje del mismo curso, programa o trámite, con su fecha.\n\n"
    "Decidí si B es la MISMA tarea que A (la persona ya la cumplió: otra foto de la misma\n"
    "consigna, la misma instrucción repetida, la misma entrega con otras palabras) o una\n"
    "asignación NUEVA que hay que hacer otra vez (otro ejercicio, otro video, otra entrega,\n"
    "aunque se parezca).\n"
    "Pistas: si B se pidió el mismo día o antes de que A se completara y describe lo mismo,\n"
    "es la misma; si B pide un material u objeto distinto (otro video, otra página, otro\n"
    "tema), es nueva.\n\n"
    'Respondé SOLO con JSON: {"same": true | false, "confidence": 0.0-1.0, '
    '"reason": "<corta>"}.'
)


@dataclass(frozen=True)
class Verdict:
    same: bool
    confidence: float
    reason: str


def parse_verdict(content: str) -> Verdict:
    """Tolerante: JSON inválido o sin `same` → «nueva» con confianza 0 (no se cubre nada)."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return Verdict(False, 0.0, "respuesta no parseable")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        return Verdict(False, 0.0, "forma inesperada")
    raw_same = data.get("same", data.get("misma"))
    if isinstance(raw_same, str):
        same = raw_same.strip().lower() in ("true", "si", "sí", "yes", "misma", "same")
    else:
        same = bool(raw_same)
    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = min(1.0, max(0.0, confidence))
    return Verdict(same, confidence, str(data.get("reason", "") or "")[:300])


def user_content(
    done: Mapping[str, Any], *, new_title: str, new_evidence: str, new_day: date | None
) -> str:
    done_meta = done.get("metadata") or {}
    return (
        "(A) Tarea ya HECHA:\n"
        f"  título: {done.get('title')}\n"
        f"  completada el: {done.get('done_at')}\n"
        f"  evidencia del pedido: {done.get('evidence') or ''}\n"
        f"  evidencia de que la hizo: {done_meta.get('done_evidence') or ''}\n\n"
        "(B) Candidata:\n"
        f"  título: {new_title}\n"
        f"  pedida el: {new_day.isoformat() if new_day else 'desconocido'}\n"
        f"  evidencia: {new_evidence}\n"
    )


async def judge_same_task(
    llm: LLMClient,
    *,
    done: Mapping[str, Any],
    new_title: str,
    new_evidence: str,
    new_day: date | None,
) -> tuple[Verdict, LLMResult]:
    msgs = [
        ChatMessage("system", JUDGE_SYSTEM_PROMPT),
        ChatMessage(
            "user",
            user_content(done, new_title=new_title, new_evidence=new_evidence, new_day=new_day),
        ),
    ]
    result = await llm.complete(
        msgs, response_format="json_object", temperature=0.0, max_tokens=_MAX_TOKENS
    )
    return parse_verdict(result.content), result


# --- misma tarea PENDIENTE leída en otra fuente ----------------------------------------------- #

PENDING_JUDGE_SYSTEM_PROMPT = (
    "Sos un juez de TAREAS personales. Te doy (A) una tarea PENDIENTE ya registrada, con el\n"
    "mensaje que la pidió, su fecha límite y su detalle, y (B) una candidata extraída de OTRO\n"
    "mensaje del mismo curso, programa o trámite (otra fuente: un chat de grupo, una nota de\n"
    "clase, una diapositiva, un correo), con su fecha y su detalle.\n\n"
    "La misma obligación suele llegar por varias vías y con otras palabras, a veces con detalles\n"
    "nuevos (cuántas, qué página, en qué formato) o con el plazo movido. Decidí si B es la MISMA\n"
    "tarea que A (la misma obligación, aunque cambie la redacción, se sumen detalles o se mueva\n"
    "la fecha) o una tarea DISTINTA (otro ejercicio, otra página, otro video, otra entrega, otro\n"
    "paso del mismo proyecto, aunque se parezca).\n"
    "Pistas: mismo objeto y misma acción = misma; material u objeto distinto = distinta; el\n"
    "número de entrega o de paso distinto = distinta.\n"
    "TIEMPO: si B se pidió DESPUÉS de que venció el plazo de A, en otra sesión u otra semana, y\n"
    "no trae una fecha nueva ni dice que es lo mismo (prórroga, recordatorio, «sigue activa»),\n"
    "es una tarea NUEVA aunque use las mismas palabras: una consigna genérica («revisar el sitio\n"
    "y tomar notas», «ver el video», «hacer los ejercicios») se repite cada sesión con otro\n"
    "material. Lo mismo si A y B son consignas genéricas pedidas en DÍAS distintos y con plazos\n"
    "distintos: una por sesión, salvo que citen el mismo material o la misma entrega.\n\n"
    'Respondé SOLO con JSON: {"same": true | false, "confidence": 0.0-1.0, '
    '"reason": "<corta>"}.'
)


def pending_user_content(
    existing: Mapping[str, Any],
    *,
    fixed_day: date | None,
    new_title: str,
    new_description: str,
    new_evidence: str,
    new_day: date | None,
    new_due: date | None,
) -> str:
    return (
        "(A) Tarea PENDIENTE registrada:\n"
        f"  título: {existing.get('title')}\n"
        f"  fecha límite: {existing.get('due_on') or 'sin fecha'}\n"
        f"  pedida el: {fixed_day.isoformat() if fixed_day else 'desconocido'}\n"
        f"  detalle: {existing.get('description') or ''}\n"
        f"  evidencia del pedido: {existing.get('evidence') or ''}\n\n"
        "(B) Candidata de otro mensaje:\n"
        f"  título: {new_title}\n"
        f"  fecha límite: {new_due.isoformat() if new_due else 'sin fecha'}\n"
        f"  pedida el: {new_day.isoformat() if new_day else 'desconocido'}\n"
        f"  detalle: {new_description}\n"
        f"  evidencia: {new_evidence}\n"
    )


async def judge_same_pending(
    llm: LLMClient,
    *,
    existing: Mapping[str, Any],
    fixed_day: date | None,
    new_title: str,
    new_description: str,
    new_evidence: str,
    new_day: date | None,
    new_due: date | None,
) -> tuple[Verdict, LLMResult]:
    msgs = [
        ChatMessage("system", PENDING_JUDGE_SYSTEM_PROMPT),
        ChatMessage(
            "user",
            pending_user_content(
                existing,
                fixed_day=fixed_day,
                new_title=new_title,
                new_description=new_description,
                new_evidence=new_evidence,
                new_day=new_day,
                new_due=new_due,
            ),
        ),
    ]
    result = await llm.complete(
        msgs, response_format="json_object", temperature=0.0, max_tokens=_MAX_TOKENS
    )
    return parse_verdict(result.content), result
