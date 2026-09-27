"""Segunda verificación de FECHAS de calendar (`CAP_VERIFY`): fase de extracción, sin DB.

Regla única y barata: un evento cuya fecha queda a **≥ `SECOND_LOOK_MONTHS` meses** (antes o
después) de la RECEPCIÓN del mensaje que lo anuncia es sospechoso y va a un juez atómico. Todo lo
demás pasa derecho, sin gasto. Un mensaje anuncia cosas de su entorno temporal; lo que cae lejos
suele ser un año copiado de una plantilla reciclada («5 de junio de 2025» en un correo de mayo de
2026), un año inventado por el extractor, o un número que no era una fecha.

El juez ve el mensaje (con su fecha de envío) y el evento extraído, y responde `keep` (la fecha es
esa, p. ej. un congreso anunciado con un año de antelación), `fix` (con la fecha correcta) o
`discard` (no hay fecha real). Solo se aplica un `fix`/`discard` con confianza ≥ `MIN_CONFIDENCE`;
ante duda o error del LLM, el item queda como vino (best-effort, se loguea). Cada corrección deja
un `VerificationNote` en el item, que `persist` cuelga de la entidad en la traza; cada llamada se
registra en `llm_calls` con `purpose="calendar_date_verify"`.

Proveedor: el juez es ARBITRAJE → consumidor propio `calendar_date_verify` del registro LLM
(`memex-llm settings set --consumer calendar_date_verify --provider codex --fallback deepseek`),
no el cliente de extracción (DeepSeek, volumen). Regla del dueño 2026-09-07.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from memex.llm import ChatMessage, LLMError, LLMQuotaError
from memex.logging import get_logger
from memex.modules.calendar.schema import CalendarEventItem
from memex.modules.contract import (
    ExtractionItem,
    VerificationNote,
    VerifyCall,
    VerifyContext,
    VerifyOutcome,
)

_log = get_logger("memex.modules.calendar.verify")

PURPOSE = "calendar_date_verify"
#: Distancia (en meses) entre la fecha del evento y la recepción del mensaje a partir de la cual
#: el evento va a segunda verificación. Decisión del dueño (2026-09-07): 9 meses, hacia atrás o
#: hacia adelante, contra la RECEPCIÓN (no contra hoy).
SECOND_LOOK_MONTHS = 9
#: Confianza mínima del juez para aplicar un `fix` o `discard`; por debajo, se conserva el item.
MIN_CONFIDENCE = 0.6
#: Tope de texto del mensaje que se le muestra al juez (el extractor vio lo mismo, entero).
_BODY_CHARS = 6000
_MAX_TOKENS = 256

VERIFY_SYSTEM_PROMPT = (
    "Sos un verificador de FECHAS de eventos extraídos de mensajes personales (correos y chats).\n"
    "Te paso UN mensaje con la fecha en que se envió (`enviado`) y UN evento que un extractor\n"
    "sacó de ese mensaje. La fecha del evento quedó lejos (meses) de la fecha del mensaje, y por\n"
    "eso hay que revisarla: un mensaje anuncia cosas de su entorno temporal.\n\n"
    "Decidí, leyendo el mensaje:\n"
    "- 'keep': la fecha extraída es la que el mensaje realmente dice y tiene sentido (p. ej. un\n"
    "  congreso anunciado con mucha antelación, un recordatorio de algo pasado).\n"
    "- 'fix': la fecha correcta es otra. Errores típicos: el mensaje trae un año viejo copiado\n"
    "  de una plantilla reciclada (el día de la semana que nombra no cuadra con ese año); el\n"
    "  extractor inventó el año; tomó un código o número (periodo académico, referencia) como si\n"
    "  fuera una fecha. Si el texto no dice el año, el coherente es el de `enviado` (o el\n"
    "  siguiente si el mes ya pasó).\n"
    "- 'discard': el mensaje NO permite fijar una fecha concreta para ese evento.\n\n"
    "Todas las fechas de un mismo mensaje son coherentes entre sí y con `enviado`: un plazo de\n"
    "inscripción cierra poco antes de su evento, y algo que el mensaje pide HACER no puede caer\n"
    "en el pasado respecto de `enviado`. Que el día de la semana cuadre con el año escrito no\n"
    "basta si ese año deja la fecha en el pasado. Si la fecha correcta es la que ya está,\n"
    "respondé 'keep', no 'fix'.\n\n"
    "Respondé SOLO con un objeto JSON con esta forma exacta:\n"
    '{"verdict": "<keep|fix|discard>", "starts_on": "<YYYY-MM-DD|null>", '
    '"ends_on": "<YYYY-MM-DD|null>", "confidence": <0.0-1.0>, "rationale": "<motivo breve>"}\n'
    "En 'fix', `starts_on` es obligatorio (y `ends_on` solo si el evento dura varios días)."
)


def _add_months(d: date, months: int) -> date:
    """`d` corrida `months` meses (negativo hacia atrás); el día se recorta al fin de mes."""
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    last = [31, 29 if _leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(d.day, last))


def _leap(y: int) -> bool:
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def needs_second_look(
    starts_on: date, received_on: date, *, months: int = SECOND_LOOK_MONTHS
) -> bool:
    """¿La fecha del evento queda a `months` meses o más (antes o después) de la recepción?"""
    return starts_on <= _add_months(received_on, -months) or starts_on >= _add_months(
        received_on, months
    )


@dataclass(frozen=True)
class Verdict:
    verdict: str  # keep | fix | discard
    starts_on: date | None
    ends_on: date | None
    confidence: float
    rationale: str


def parse_verdict(content: str) -> Verdict:
    """Parsea la respuesta del juez. Cualquier ambigüedad → `keep` (sesgo a no tocar)."""
    keep = Verdict("keep", None, None, 0.0, "parse_fallback")
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return keep
    if not isinstance(data, dict):
        return keep
    verdict = str(data.get("verdict") or "keep").strip().lower()
    if verdict not in ("keep", "fix", "discard"):
        verdict = "keep"
    try:
        conf = max(0.0, min(1.0, float(data.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        conf = 0.0
    starts_on = _parse_date(data.get("starts_on"))
    ends_on = _parse_date(data.get("ends_on"))
    if verdict == "fix" and starts_on is None:
        return Verdict("keep", None, None, conf, "fix_without_date")
    return Verdict(verdict, starts_on, ends_on, conf, str(data.get("rationale") or ""))


def _parse_date(v: object) -> date | None:
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        return date.fromisoformat(v.strip()[:10])
    except ValueError:
        return None


def _received_on(e: CalendarEventItem, ctx: VerifyContext) -> date | None:
    """Recepción más tardía de los mensajes citados por el evento (None si no se conoce)."""
    stamps: list[datetime] = [
        ctx.occurred_at_by_id[i] for i in e.source_inbox_ids if i in ctx.occurred_at_by_id
    ]
    return max(stamps).date() if stamps else None


def _user_content(e: CalendarEventItem, ctx: VerifyContext, received: date) -> str:
    text = "\n\n".join(ctx.rendered_by_id.get(i, "") for i in e.source_inbox_ids).strip()
    if len(text) > _BODY_CHARS:
        text = text[:_BODY_CHARS] + " […]"
    event = {
        "title": e.title,
        "starts_on": e.starts_on.isoformat(),
        "ends_on": e.ends_on.isoformat() if e.ends_on else None,
        "evidence": e.evidence,
    }
    return (
        f"enviado: {received.isoformat()}\n"
        f"evento extraído: {json.dumps(event, ensure_ascii=False)}\n\n"
        f"mensaje:\n{text}"
    )


async def verify_dates(items: Sequence[ExtractionItem], ctx: VerifyContext) -> VerifyOutcome:
    """Segunda verificación de calendar: solo los eventos a ≥ 9 meses de la recepción van al juez;
    el resto pasa igual. Devuelve los items resultantes + las llamadas LLM para registrar."""
    out: list[ExtractionItem] = []
    calls: list[VerifyCall] = []
    llm = None  # perezoso: solo se construye si algún evento cae en la regla
    for it in items:
        if not isinstance(it, CalendarEventItem):
            out.append(it)
            continue
        received = _received_on(it, ctx)
        if received is None or not needs_second_look(it.starts_on, received):
            out.append(it)
            continue
        inbox_id = it.source_inbox_ids[0] if it.source_inbox_ids else None
        msgs = [
            ChatMessage("system", VERIFY_SYSTEM_PROMPT),
            ChatMessage("user", _user_content(it, ctx, received)),
        ]
        if llm is None:
            llm = ctx.client_for(PURPOSE)
        try:
            result = await llm.complete(
                msgs, response_format="json_object", temperature=0.0, max_tokens=_MAX_TOKENS
            )
        except LLMQuotaError:
            raise  # saldo agotado: que aborte el run, como en extracción
        except LLMError as exc:
            _log.warning(
                "calendar.verify.llm_error", inbox_id=inbox_id, title=it.title, error=str(exc)
            )
            out.append(it)  # best-effort: sin juez, el item queda como vino
            continue
        verdict = parse_verdict(result.content)
        applied = verdict.verdict if verdict.confidence >= MIN_CONFIDENCE else "keep"
        if applied == "fix" and verdict.starts_on == it.starts_on and verdict.ends_on == it.ends_on:
            applied = "keep"  # un 'fix' que no cambia nada es un keep (metadata honesta)
        calls.append(
            VerifyCall(
                purpose=PURPOSE,
                result=result,
                inbox_id=inbox_id,
                metadata={
                    "title": it.title,
                    "starts_on": it.starts_on.isoformat(),
                    "received_on": received.isoformat(),
                    "verdict": verdict.verdict,
                    "applied": applied,
                    "confidence": verdict.confidence,
                },
            )
        )
        _log.info(
            "calendar.verify.judged",
            inbox_id=inbox_id,
            title=it.title,
            starts_on=it.starts_on.isoformat(),
            received_on=received.isoformat(),
            verdict=verdict.verdict,
            applied=applied,
            confidence=verdict.confidence,
            rationale=verdict.rationale,
        )
        if applied == "discard":
            continue
        if (
            applied == "fix"
            and verdict.starts_on is not None
            and (verdict.starts_on != it.starts_on or verdict.ends_on != it.ends_on)
        ):
            note = VerificationNote(
                field="starts_on",
                original=it.starts_on.isoformat(),
                corrected=verdict.starts_on.isoformat(),
                rationale=verdict.rationale,
                confidence=verdict.confidence,
            )
            out.append(
                it.model_copy(
                    update={
                        "starts_on": verdict.starts_on,
                        "ends_on": verdict.ends_on,
                        "verification": note,
                    }
                )
            )
            continue
        out.append(it)
    return VerifyOutcome(items=out, calls=calls)
