"""`TareasModule` — extractor de TAREAS (lo que hay que hacer) desde los mensajes.

Hasta 2026-09-13 `tareas` era solo un registrador manual (`module.add`, por el bot o la consola):
ningún mensaje generaba una tarea solo. Este módulo lo vuelve un extractor como `calendar`: el LLM
lee la ventana con el prompt de tareas, y cada item validado entra por la MISMA puerta que una
tarea manual (`module.add`: dedup por clave natural + difusa, vértice, arista al asunto), con su
procedencia (`source_inbox_ids`, `evidence`, `origin='extraction'`).

Asunto: la extracción corre ANTES que la asignación de asuntos de ese mismo mensaje, así que la
tarea nace suelta salvo que el mensaje ya tenga evidencia de un asunto (re-extracción) —
`_asunto_for_inbox`— o que `asunto_hint` resuelva a un asunto conocido por nombre/alias. Cuando
la etapa de asuntos asienta evidencia sobre un mensaje, adopta las tareas sueltas que salieron de
él (`module.adopt_orphan_tareas`). Las dos direcciones cubren el orden en que llegue cada cosa.

En chats de grupo el orquestador solo enruta este módulo si la ventana tiene naturaleza `intimo` o
`programa` (donde la persona participa); en una comunidad de `anuncios` las obligaciones son de
otros.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, ClassVar

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.localtime import LOCAL_TZ
from memex.core.observability import record_llm_call
from memex.core.source import HealthResult, SourceKind
from memex.db import connection
from memex.llm import LLMClient, LLMError, LLMQuotaError
from memex.llm.config import LLMConfigError
from memex.llm.registry import aclose_llm, build_llm_client
from memex.logging import get_logger
from memex.modules.asuntos.datos import SecretDatoError, add_dato
from memex.modules.contract import CAP_EXTRACT, ExtractionItem, ModuleContext
from memex.modules.tareas import judge as tareas_judge
from memex.modules.tareas.module import (
    STATUS_PENDIENTE,
    STATUS_VENCIDA,
    AsuntoNotFoundError,
    Duplicate,
    SimilarCandidate,
    add,
    attach_links,
    clear_dedup_pending,
    complete_from_evidence,
    enrich,
    expire_overdue,
    family_of,
    find_completed_cover,
    find_duplicate,
    find_live_semantic,
    find_or_create_parent,
    find_parent,
    find_similar_candidates,
    find_task_by_title,
    loose_steps_with_hint,
    merge_provenance,
    merge_tareas,
    resolve_asunto,
    set_parent,
)
from memex.modules.tareas.prompt import TAREAS_SYSTEM_PROMPT
from memex.modules.tareas.ritmo import plazo_tipico
from memex.modules.tareas.schema import TareaItem

_log = get_logger("memex.modules.tareas.extract")


#: Marcas de que un pedido, aunque venga como respuesta a alguien, es para TODO el grupo.
_COLLECTIVE_MARKERS = (
    "todos",
    "todas",
    "cada uno",
    "cada una",
    "el grupo",
    "al grupo",
    "los participantes",
    "las participantes",
    "everyone",
    "everybody",
)


def _sender_of(payload: Mapping[str, Any]) -> dict[str, Any]:
    snd = payload.get("sender")
    return dict(snd) if isinstance(snd, dict) else {}


def _addressed_to_someone_else(conn: Connection, inbox_ids: Sequence[int]) -> bool:
    """¿La tarea sale SOLO de respuestas a OTRA persona? Un mensaje de chat que responde al
    mensaje de un tercero (ni del usuario ni de sí mismo) le pide a ese tercero (principio 3: la
    señal pertenece al hilo). Si alguna fuente no es una respuesta, o responde al propio usuario,
    o el texto habla al grupo («todos», «cada uno»), no aplica. Sin fuentes: no aplica."""
    ids = [int(i) for i in inbox_ids]
    if not ids:
        return False
    rows = conn.execute(
        text("SELECT id, source_id, payload FROM inbox WHERE id = ANY(:ids)"), {"ids": ids}
    ).all()
    if len(rows) != len(ids):
        return False
    for _iid, source_id, payload in rows:
        p = payload or {}
        target = p.get("reply_to_message_id")
        if not target or "chat_id" not in p:
            return False
        snd = _sender_of(p)
        if snd.get("is_self"):
            return False
        low = " ".join(str(p.get("text") or p.get("media_caption") or "").lower().split())
        if any(m in low for m in _COLLECTIVE_MARKERS):
            return False
        orig = conn.execute(
            text(
                "SELECT payload FROM inbox WHERE source_id = :s AND payload->>'chat_id' = :c "
                "AND payload->>'message_id' = :m ORDER BY id LIMIT 1"
            ),
            {"s": int(source_id), "c": str(p.get("chat_id")), "m": str(target)},
        ).scalar()
        if not isinstance(orig, dict):
            return False  # sin el original no se sabe a quién le habla
        osnd = _sender_of(orig)
        if osnd.get("is_self"):
            return False  # le responde al propio usuario: sí es para la persona
        same_person = (
            snd.get("user_id") is not None and snd.get("user_id") == osnd.get("user_id")
        ) or (snd.get("phone") is not None and snd.get("phone") == osnd.get("phone"))
        if same_person:
            return False  # se responde a sí misma (hilo propio): no es «a otra persona»
    return True


def _asunto_for_inbox(conn: Connection, user_id: int, inbox_ids: Sequence[int]) -> int | None:
    """El asunto con evidencia ACTIVA sobre alguno de los mensajes citados, si ya lo hay."""
    val = conn.execute(
        text(
            "SELECT asunto_id FROM mod_asuntos_evidence "
            "WHERE user_id = :u AND status = 'active' AND inbox_id = ANY(:ids) "
            "ORDER BY confidence DESC, id LIMIT 1"
        ),
        {"u": user_id, "ids": list(inbox_ids)},
    ).scalar()
    return int(val) if val is not None else None


#: Una dinámica o consigna de chat sin plazo dicho vale esta cantidad de días desde el mensaje.
UNA_SEMANA_DIAS = 7
#: Un aviso de seguridad (intento de acceso, 2FA) sin plazo dicho vale estos días: después ya no
#: hay nada que verificar y la tarea vence sola en vez de quedar pendiente para siempre.
SEGURIDAD_DIAS = 2

#: Un chat «pertenece» a un asunto cuando casi toda su evidencia apunta a ese asunto.
CHAT_ASUNTO_MIN_EVIDENCE = 3
CHAT_ASUNTO_MIN_SHARE = 0.8


def _asunto_for_chat(conn: Connection, user_id: int, inbox_ids: Sequence[int]) -> int | None:
    """Determinista: el asunto DOMINANTE del chat de donde salió la tarea. En un grupo de curso o
    de programa, lo que el docente pide es del curso aunque el asignador no haya decidido ese
    mensaje en particular (una foto del tablero, p. ej.). Se exige que el chat tenga evidencia
    suficiente y que un solo asunto se lleve casi toda; si el chat mezcla asuntos, None."""
    if not inbox_ids:
        return None
    row = conn.execute(
        text(
            """
            WITH chat AS (
                SELECT DISTINCT source_id, payload->>'chat_id' AS chat_id
                  FROM inbox WHERE id = ANY(:ids) AND payload ? 'chat_id'
            ),
            ev AS (
                SELECT e.asunto_id, count(*) AS n
                  FROM mod_asuntos_evidence e
                  JOIN inbox i ON i.id = e.inbox_id
                  JOIN chat c ON c.source_id = i.source_id AND c.chat_id = i.payload->>'chat_id'
                  JOIN mod_asuntos a ON a.id = e.asunto_id AND a.merged_into_id IS NULL
                 WHERE e.user_id = :u AND e.status = 'active'
                 GROUP BY e.asunto_id
            )
            SELECT asunto_id, n, (SELECT sum(n) FROM ev) AS total
              FROM ev ORDER BY n DESC LIMIT 1
            """
        ),
        {"ids": list(inbox_ids), "u": user_id},
    ).first()
    if row is None:
        return None
    asunto_id, n, total = int(row[0]), int(row[1]), int(row[2])
    if n < CHAT_ASUNTO_MIN_EVIDENCE or n < CHAT_ASUNTO_MIN_SHARE * total:
        return None
    return asunto_id


def resolve_asunto_for(
    conn: Connection, user_id: int, inbox_ids: Sequence[int], hint: str
) -> int | None:
    """Orden: evidencia del propio mensaje → nombre que dio el LLM si nombra un asunto de la
    FAMILIA del dominante del chat → asunto dominante del chat → nombre que dio el LLM.

    Principio 3 (la señal pertenece al hilo): en el chat de un programa conviven el programa y
    las cosas que son parte de él (la beca para el evento, el viaje). Antes el chat dominante
    ganaba siempre y la tarea del correo de la beca y la del chat sobre esa misma beca caían en
    asuntos distintos (y el dedup, por asunto, no las veía). El nombre que da el modelo manda
    cuando apunta a un asunto relacionado con el del chat; si apunta a cualquier otra cosa, no
    (un «contiene» flojo no saca la tarea de su chat)."""
    asunto_id = _asunto_for_inbox(conn, user_id, inbox_ids)
    if asunto_id is not None:
        return asunto_id
    dominant = _asunto_for_chat(conn, user_id, inbox_ids)
    hinted = _asunto_by_hint(conn, user_id, hint)
    if dominant is None:
        return hinted
    if hinted is not None and hinted != dominant and hinted in family_of(conn, user_id, dominant):
        return hinted
    return dominant


def _judge_client(user_id: int) -> LLMClient:
    """Cliente del juez (`tareas_judge` → codex). Módulo-nivel para que los tests lo reemplacen."""
    return build_llm_client(tareas_judge.PURPOSE, user_id=user_id)


@dataclass
class RecheckStats:
    """Qué hizo el re-chequeo de duplicados."""

    revisadas: int = 0
    juzgadas: int = 0
    fundidas: int = 0
    sin_juez: int = 0
    pares: list[dict[str, Any]] = field(default_factory=list)


async def recheck_duplicates(
    user_id: int,
    *,
    tarea_ids: Sequence[int] | None = None,
    since: date | None = None,
    apply: bool = True,
) -> RecheckStats:
    """Vuelve a buscar gemelas para tareas que se colgaron de su asunto DESPUÉS de extraídas
    (`metadata.dedup_pending`, puesto por `adopt_orphan_tareas`), o para las que se pidan por id o
    por fecha de alta. Misma regla barata (`find_similar_candidates`) y mismo juez que la
    extracción; si el juez dice que es la misma, `merge_tareas` funde la revisada en la que ya
    existía. Sin juez disponible no se funde nada. Con `apply=False` solo cuenta."""
    stats = RecheckStats()
    where = [
        "t.user_id = :u",
        "t.status = 'pendiente'",
        "t.origin = 'extraction'",
        "COALESCE((t.metadata->>'is_parent')::boolean, false) IS NOT TRUE",
    ]
    params: dict[str, Any] = {"u": user_id}
    if tarea_ids:
        where.append("t.id = ANY(:ids)")
        params["ids"] = [int(i) for i in tarea_ids]
    elif since is not None:
        where.append("t.created_at >= :since")
        params["since"] = datetime.combine(since, time.min, tzinfo=LOCAL_TZ)
    else:
        where.append("COALESCE((t.metadata->>'dedup_pending')::boolean, false) IS TRUE")
    with connection() as conn:
        targets = [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT t.id, t.title, t.description, t.evidence, t.due_on, t.asunto_id, "
                    "t.parent_id, t.source_inbox_ids, t.metadata FROM mod_tareas t WHERE "
                    + " AND ".join(where)
                    + " ORDER BY t.id"
                ),
                params,
            ).mappings()
        ]
    if not targets:
        return stats
    judge = _JudgeBox(user_id)
    try:
        for t in targets:
            stats.revisadas += 1
            tid = int(t["id"])
            ids = [int(i) for i in (t.get("source_inbox_ids") or [])]
            due = t.get("due_on")
            with connection() as conn:
                cands = [
                    c
                    for c in find_similar_candidates(
                        conn,
                        user_id,
                        title=str(t["title"]),
                        asunto_id=t.get("asunto_id"),
                        due_on=due,
                        source_inbox_ids=ids,
                        parent_id=t.get("parent_id"),
                        fixed_day=_fixed_day_of(conn, t),
                        exclude_ids=[tid],
                    )
                ]
                day = _message_day(conn, ids)
                fixed = {int(c.tarea["id"]): _fixed_day_of(conn, c.tarea) for c in cands}
                # Lo ya HECHO también cubre a una pendiente que se creó de más (un paso del
                # plan completado después, con otras palabras): mismo juez que la extracción.
                cover = find_completed_cover(
                    conn,
                    user_id,
                    title=str(t["title"]),
                    asunto_id=t.get("asunto_id"),
                    message_day=day,
                )
            merged_into: int | None = None
            if cover is not None:
                same_cover = cover.exact
                if not same_cover:
                    llm = judge.get()
                    if llm is None:
                        stats.sin_juez += 1
                    else:
                        try:
                            verdict, result = await tareas_judge.judge_same_task(
                                llm,
                                done=cover.tarea,
                                new_title=str(t["title"]),
                                new_evidence=str(t.get("evidence") or ""),
                                new_day=day,
                            )
                        except LLMQuotaError:
                            raise
                        except LLMError as exc:
                            _log.warning(
                                "tareas.recheck.llm_error",
                                user_id=user_id,
                                tarea_id=tid,
                                error=str(exc),
                            )
                        else:
                            stats.juzgadas += 1
                            same_cover = (
                                verdict.same and verdict.confidence >= tareas_judge.MIN_CONFIDENCE
                            )
                            record_llm_call(
                                user_id=user_id,
                                purpose=tareas_judge.PURPOSE,
                                model=result.model,
                                prompt_tokens=result.usage.prompt_tokens,
                                completion_tokens=result.usage.completion_tokens,
                                cost_usd=result.cost_usd,
                                latency_ms=result.latency_ms,
                                status="ok",
                                cache_hit_tokens=result.usage.cache_hit_tokens,
                                metadata={
                                    "kind": "recheck_cover",
                                    "inbox_ids": ids,
                                    "done_tarea_id": int(cover.tarea["id"]),
                                    "done_title": cover.tarea.get("title"),
                                    "new_tarea_id": tid,
                                    "new_title": t["title"],
                                    "score": cover.score,
                                    "semantic": cover.semantic,
                                    "same": verdict.same,
                                    "confidence": verdict.confidence,
                                    "applied": same_cover and apply,
                                    "reason": verdict.reason,
                                },
                                response_text=result.content,
                            )
                if same_cover:
                    merged_into = int(cover.tarea["id"])
            for cand in cands:
                if merged_into is not None:
                    break
                llm = judge.get()
                if llm is None:
                    stats.sin_juez += 1
                    break
                try:
                    verdict, result = await tareas_judge.judge_same_pending(
                        llm,
                        existing=cand.tarea,
                        fixed_day=fixed[int(cand.tarea["id"])],
                        new_title=str(t["title"]),
                        new_description=str(t.get("description") or ""),
                        new_evidence=str(t.get("evidence") or ""),
                        new_day=day,
                        new_due=due,
                    )
                except LLMQuotaError:
                    raise
                except LLMError as exc:
                    _log.warning(
                        "tareas.recheck.llm_error", user_id=user_id, tarea_id=tid, error=str(exc)
                    )
                    break
                stats.juzgadas += 1
                same = verdict.same and verdict.confidence >= tareas_judge.MIN_CONFIDENCE
                record_llm_call(
                    user_id=user_id,
                    purpose=tareas_judge.PURPOSE,
                    model=result.model,
                    prompt_tokens=result.usage.prompt_tokens,
                    completion_tokens=result.usage.completion_tokens,
                    cost_usd=result.cost_usd,
                    latency_ms=result.latency_ms,
                    status="ok",
                    cache_hit_tokens=result.usage.cache_hit_tokens,
                    metadata={
                        "kind": "recheck",
                        "inbox_ids": ids,
                        "existing_tarea_id": int(cand.tarea["id"]),
                        "existing_title": cand.tarea.get("title"),
                        "new_tarea_id": tid,
                        "new_title": t["title"],
                        "score": cand.score,
                        "semantic": cand.semantic,
                        "plan_step": cand.plan_step,
                        "same": verdict.same,
                        "confidence": verdict.confidence,
                        "applied": same and apply,
                        "reason": verdict.reason,
                    },
                    response_text=result.content,
                )
                if same:
                    merged_into = int(cand.tarea["id"])
                    break
            stats.pares.append(
                {
                    "tarea_id": tid,
                    "title": t["title"],
                    "into": merged_into,
                    "candidatas": [int(c.tarea["id"]) for c in cands],
                }
            )
            if not apply:
                continue
            with connection() as conn:
                if merged_into is not None:
                    merge_tareas(conn, user_id, into=merged_into, absorb=tid)
                    stats.fundidas += 1
                clear_dedup_pending(conn, user_id, [tid])
    finally:
        await judge.close()
    _log.info(
        "tareas.recheck.done",
        user_id=user_id,
        revisadas=stats.revisadas,
        juzgadas=stats.juzgadas,
        fundidas=stats.fundidas,
        sin_juez=stats.sin_juez,
        applied=apply,
    )
    return stats


class _JudgeBox:
    """El juez se construye una vez por corrida y solo si hace falta. Sin proveedor configurado
    (LLMConfigError) no hay juez: se sigue determinista (nada se funde por sospecha) y se avisa
    una sola vez."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.client: LLMClient | None = None
        self.unavailable = False

    def get(self) -> LLMClient | None:
        if self.client is None and not self.unavailable:
            try:
                self.client = _judge_client(self.user_id)
            except LLMConfigError as exc:
                self.unavailable = True
                _log.warning("tareas.judge.unavailable", user_id=self.user_id, error=str(exc))
        return self.client

    async def close(self) -> None:
        if self.client is not None:
            await aclose_llm(self.client)


def _resource_target(
    conn: Connection,
    user_id: int,
    it: TareaItem,
    asunto_id: int | None,
    parents: dict[str, int],
) -> int | None:
    """A qué tarea se pega un `recurso`: el proyecto ya resuelto en esta pasada, un proyecto
    existente con ese nombre, o la tarea (viva o hecha) que el nombre describe. None si nada."""
    hint = " ".join(it.parent_hint.split())
    if not hint:
        return None
    known = parents.get(hint.lower())
    if known is not None:
        return known
    parent = find_parent(conn, user_id, title=hint, asunto_id=asunto_id)
    if parent is not None:
        parents[hint.lower()] = parent
        return parent
    task = find_task_by_title(
        conn,
        user_id,
        title=hint,
        asunto_id=asunto_id,
        statuses=("pendiente", "vencida", "completada"),
    )
    return int(task["id"]) if task is not None else None


def _message_at(conn: Connection, inbox_ids: Sequence[int]) -> datetime | None:
    """Instante (UTC) del mensaje más reciente de los que salió el item."""
    if not inbox_ids:
        return None
    ts = conn.execute(
        text("SELECT max(occurred_at) FROM inbox WHERE id = ANY(:ids)"), {"ids": list(inbox_ids)}
    ).scalar()
    if not isinstance(ts, datetime):
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _from_document(conn: Connection, inbox_ids: Sequence[int]) -> bool:
    """True si el item salió de un DOCUMENTO subido (payload con `captured_from`): su fecha es la
    de la subida, no la de la clase → no sirve para «próxima sesión» ni para cubrir completadas."""
    if not inbox_ids:
        return False
    n = conn.execute(
        text("SELECT count(*) FROM inbox WHERE id = ANY(:ids) AND payload ? 'captured_from'"),
        {"ids": list(inbox_ids)},
    ).scalar()
    return bool(n)


def _message_day(conn: Connection, inbox_ids: Sequence[int]) -> date | None:
    """Día (hora local) del mensaje más reciente de los que salió la tarea."""
    if not inbox_ids:
        return None
    ts = conn.execute(
        text("SELECT max(occurred_at) FROM inbox WHERE id = ANY(:ids)"), {"ids": list(inbox_ids)}
    ).scalar()
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(LOCAL_TZ).date()


def _course_events(conn: Connection, user_id: int, asunto_id: int) -> list[Any]:
    """Eventos recurrentes CONFIRMADOS del asunto (el horario del curso): ligados por evidencia
    compartida, o manuales/sincronizados con el mismo título que uno ligado."""
    return list(
        conn.execute(
            text(
                """
            WITH ligados AS (
                SELECT e.id, e.title FROM mod_calendar_events e
                 WHERE e.user_id = :u AND e.commitment = 'confirmado'
                   AND cardinality(e.recurrence) > 0
                   AND EXISTS (SELECT 1 FROM mod_asuntos_evidence ev
                                WHERE ev.user_id = :u AND ev.asunto_id = :a AND ev.status = 'active'
                                  AND ev.inbox_id = ANY(e.source_inbox_ids))
            )
            SELECT e.id, e.starts_on, e.start_time, e.recurrence
              FROM mod_calendar_events e
             WHERE e.user_id = :u AND e.commitment = 'confirmado'
               AND cardinality(e.recurrence) > 0
               AND (e.id IN (SELECT id FROM ligados)
                    OR memex_norm(e.title) IN (SELECT memex_norm(title) FROM ligados))
            """
            ),
            {"u": user_id, "a": asunto_id},
        ).all()
    )


def course_session_dates(conn: Connection, user_id: int, asunto_id: int) -> list[date]:
    """Los días de clase del asunto, en orden: la unión de las ocurrencias de su horario (eventos
    recurrentes confirmados) desde que empieza, hasta 200 días. Vacío si no hay horario."""
    from memex.modules.calendar.recurrence import occurrences

    days: set[date] = set()
    for _eid, starts_on, start_time, recurrence in _course_events(conn, user_id, asunto_id):
        days.update(
            occurrences(
                starts_on,
                list(recurrence or ()),
                window_start=starts_on,
                window_end=starts_on + timedelta(days=200),
                start_time=start_time,
            )
        )
    return sorted(days)


_TRAILING_NUMBER = re.compile(r"(\d{1,3})\s*$")
_SESSION_WORD = re.compile(
    r"(?:clase|sesi[oó]n|session|lesson|class|lecci[oó]n|d[ií]a|semana|week)\s*[-_ ]?\s*(\d{1,3})",
    re.IGNORECASE,
)


def session_index_from_name(filename: str) -> int | None:
    """El número de sesión que trae el nombre de un archivo de curso: «3.pdf», «B1-1_10.pdf»,
    «Clase 7 - pasado.pdf», «sesion_12.pdf». Un «(1)» de descarga repetida no cuenta. None si no
    hay número claro (programa.pdf, quiz.pdf)."""
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    stem = re.sub(r"\(\d+\)", " ", stem).strip()
    m = _SESSION_WORD.search(stem)
    if m:
        return int(m.group(1))
    m = _TRAILING_NUMBER.search(stem)
    if m:
        return int(m.group(1))
    return None


def _document_session_day(
    conn: Connection, user_id: int, asunto_id: int, inbox_ids: Sequence[int]
) -> date | None:
    """Día de clase de un DOCUMENTO del asunto: la lámina k (por el número del nombre del archivo)
    es la k-ésima sesión del horario. None si el archivo no trae número, el asunto no tiene horario
    o k se pasa de las sesiones."""
    names = conn.execute(
        text(
            "SELECT payload->>'filename' FROM inbox "
            "WHERE id = ANY(:ids) AND payload ? 'captured_from' ORDER BY id"
        ),
        {"ids": list(inbox_ids)},
    ).scalars()
    for name in names:
        k = session_index_from_name(str(name or ""))
        if k is None:
            continue
        days = course_session_dates(conn, user_id, asunto_id)
        if 1 <= k <= len(days):
            return days[k - 1]
    return None


def _next_session(conn: Connection, user_id: int, asunto_id: int, after: date) -> date | None:
    """Próxima sesión del asunto DESPUÉS de `after`: la ocurrencia más cercana de los eventos
    recurrentes confirmados ligados al asunto (por evidencia compartida o manuales/sincronizados con
    el mismo título que uno ligado). Sin calendario del curso → None (la tarea queda sin fecha)."""
    from memex.modules.calendar.recurrence import next_occurrences

    best: date | None = None
    for _eid, starts_on, start_time, recurrence in _course_events(conn, user_id, asunto_id):
        nxt = next_occurrences(
            starts_on,
            list(recurrence or ()),
            after=after + timedelta(days=1),
            limit=1,
            start_time=start_time,
            horizon_days=60,
        )
        if nxt and (best is None or nxt[0] < best):
            best = nxt[0]
    return best


def _asunto_by_hint(conn: Connection, user_id: int, hint: str) -> int | None:
    if not hint.strip():
        return None
    try:
        return resolve_asunto(conn, user_id, hint)
    except AsuntoNotFoundError:
        return None


def _fixed_day_of(conn: Connection, tarea: Mapping[str, Any]) -> date | None:
    """Cuándo se pidió la tarea existente: el día que fijó su fecha (`metadata.due_fixed_day`), si
    no la sesión de la lámina (`session_day`), si no el mensaje más reciente de su procedencia."""
    meta = tarea.get("metadata") or {}
    for key in ("due_fixed_day", "session_day"):
        raw = meta.get(key)
        if raw:
            try:
                return date.fromisoformat(str(raw))
            except ValueError:
                pass
    return _message_day(conn, [int(i) for i in (tarea.get("source_inbox_ids") or [])])


class TareasModule:
    """Extrae tareas de los mensajes y las registra por `module.add` (dedup incluido)."""

    slug: ClassVar[str] = "tareas"
    interest: ClassVar[str] = (
        "Cosas que la persona tiene que HACER (tareas, pendientes): trabajos y ejercicios para "
        "entregar, documentos que enviar, inscripciones o postulaciones que completar, pagos o "
        "trámites por hacer, cosas que le pidieron o que ella dijo que haría, con su fecha "
        "límite si la hay; las DINÁMICAS, retos o desafíos de un programa o comunidad que dan "
        "puntos (responder un acertijo, publicar con un hashtag, mandar una captura, cada una "
        "con su plazo); también la confirmación de que ya hizo algo, los enlaces o archivos "
        "de un proyecto en curso (plantilla, video, carpeta) y los DATOS que hacen falta para "
        "participar (código de clase o de acceso de una plataforma, enlace de reunión, salón, "
        "número de expediente o de reserva); y los AVISOS DE SEGURIDAD de una cuenta propia "
        "(intento de acceso, dispositivo o ubicación nueva, cambio de contraseña, restablecer "
        "2FA, actividad sospechosa): la persona tiene que verificar que fue ella. NO eventos "
        "con hora (eso es calendario) ni anuncios generales."
    )
    extraction_schema: ClassVar[type[ExtractionItem]] = TareaItem
    extraction_prompt: ClassVar[str] = TAREAS_SYSTEM_PROMPT
    capabilities: ClassVar[frozenset[str]] = frozenset({CAP_EXTRACT})
    consumes_kinds: ClassVar[frozenset[SourceKind]] = frozenset(
        {SourceKind.EMAIL, SourceKind.CHAT, SourceKind.NOTES, SourceKind.DOCUMENTOS}
    )
    depends_on: ClassVar[tuple[str, ...]] = ()
    optional_deps: ClassVar[tuple[str, ...]] = ()
    #: `()` = dedup por MECANISMO PROPIO: `module.add` (clave natural + difusa entre pendientes).
    identity_fields: ClassVar[tuple[str, ...]] = ()
    #: Consumer LLM propio: la extracción de tareas corre en su propia unidad y su proveedor se
    #: configura aparte (`memex-llm settings set --consumer extract_tareas …`): el dueño pidió
    #: que esta parte funcione bien aunque haya que ir a codex.
    llm_consumer: ClassVar[str | None] = "extract_tareas"

    async def persist(self, ctx: ModuleContext, items: Sequence[ExtractionItem]) -> int:
        return await self.dedup(ctx, items)

    async def dedup(self, ctx: ModuleContext, items: Sequence[ExtractionItem]) -> int:
        tareas = [i for i in items if isinstance(i, TareaItem)]
        if not tareas:
            return 0
        inserted = deduped = 0
        # Los ítems de una MISMA lista comparten su marco: si «preparación para la clase» marcó un
        # ítem como «para la próxima sesión», sus hermanos sin fecha del mismo mensaje también lo
        # son (el LLM a veces lo marca en uno solo).
        next_session_msgs = {
            i for t in tareas if t.due_kind == "proxima_sesion" for i in t.source_inbox_ids
        }
        # Proyectos ya resueltos en esta pasada (nombre normalizado → id del padre).
        parents: dict[str, int] = {}
        # Cuántos ítems de esta pasada nombran cada proyecto (pasos y recursos: la plantilla del
        # proyecto también prueba que existe): un proyecto nace con dos o más.
        hint_counts = Counter(
            " ".join(t.parent_hint.lower().split())
            for t in tareas
            if t.parent_hint and t.kind in ("tarea", "recurso")
        )
        judge = _JudgeBox(ctx.user_id)
        for it in tareas:
            asunto_id = resolve_asunto_for(
                ctx.conn, ctx.user_id, it.source_inbox_ids, it.asunto_hint
            )
            from_doc = _from_document(ctx.conn, it.source_inbox_ids)
            if from_doc:
                # Un documento no tiene «fecha del mensaje» (la de subida no dice nada de la
                # clase); si es la lámina k de un curso con horario, su día es la sesión k.
                day = (
                    _document_session_day(ctx.conn, ctx.user_id, asunto_id, it.source_inbox_ids)
                    if asunto_id is not None
                    else None
                )
            else:
                day = _message_day(ctx.conn, it.source_inbox_ids)
            if it.kind == "tarea" and _addressed_to_someone_else(ctx.conn, it.source_inbox_ids):
                # Principio 3: una respuesta a OTRA persona le pide a esa persona. El modelo lo ve
                # («↩ en respuesta a X») y aun así lo anota como tarea propia (2026-09-26,
                # «confírmame nombre, país y estrategia»): regla determinista, no depende de él.
                _log.info(
                    "tareas.skip.addressed_to_other",
                    user_id=ctx.user_id,
                    title=it.title[:80],
                    inbox_ids=list(it.source_inbox_ids),
                )
                continue
            if it.kind == "dato":
                # Un hecho pequeño para tener a mano (código de clase, enlace de acceso): va a los
                # datos del asunto, no crea tarea.
                if asunto_id is None or not it.value:
                    _log.info(
                        "tareas.dato.sin_destino",
                        user_id=ctx.user_id,
                        title=it.title[:80],
                        inbox_ids=list(it.source_inbox_ids),
                    )
                    continue
                try:
                    nuevo = add_dato(
                        ctx.conn,
                        ctx.user_id,
                        asunto_id,
                        label=it.title,
                        value=it.value,
                        source_inbox_ids=it.source_inbox_ids,
                        evidence=it.evidence,
                    )
                except SecretDatoError:
                    # Una contraseña o clave temporal: se queda en el mensaje, no en el asunto.
                    _log.info(
                        "tareas.dato.secreto_omitido",
                        user_id=ctx.user_id,
                        label=it.title[:60],
                        inbox_ids=list(it.source_inbox_ids),
                    )
                    continue
                ent = ctx.trace.entity(
                    "mod_asuntos",
                    id=asunto_id,
                    label=f"dato: {it.title} = {it.value}",
                    status="ok",
                    source_inbox_ids=it.source_inbox_ids,
                )
                ent.log(
                    "dato nuevo del asunto" if nuevo else "el asunto ya tenía este dato",
                    status="info",
                )
                continue
            if it.kind == "hecha":
                # La entrega/confirmación (propia, o del organizador dirigida a la persona):
                # completa la tarea que cumple (o la registra ya hecha). No crea pendientes. Si
                # por título no hay viva que corresponda, la vecina por SIGNIFICADO de la familia
                # del asunto pasa por el juez («you're on the list for X» ↔ «inscribirte en X»).
                target_id: int | None = None
                by_title = find_task_by_title(
                    ctx.conn,
                    ctx.user_id,
                    title=it.title,
                    asunto_id=asunto_id,
                    statuses=(STATUS_PENDIENTE, STATUS_VENCIDA),
                )
                if by_title is None:
                    near = find_live_semantic(
                        ctx.conn, ctx.user_id, title=it.title, asunto_id=asunto_id
                    )
                    llm = judge.get() if near is not None else None
                    if near is not None and llm is not None:
                        cand = SimilarCandidate(near[0], near[1], False, False, semantic=True)
                        if await self._judge_pending(ctx, llm, cand, it, day, None):
                            target_id = int(near[0]["id"])
                    if target_id is None:
                        # Ya la tenía HECHA con otras palabras (la confirmación repetida, o el
                        # paso marcado a mano): se suma la procedencia, no nace una segunda
                        # completada. Misma cobertura y mismo juez que una candidata normal.
                        cover = find_completed_cover(
                            ctx.conn,
                            ctx.user_id,
                            title=it.title,
                            asunto_id=asunto_id,
                            message_day=day,
                        )
                        if cover is not None:
                            llm = judge.get() if not cover.exact else None
                            if cover.exact or (
                                llm is not None
                                and await self._judge_cover(ctx, llm, cover, it, day)
                            ):
                                target_id = int(cover.tarea["id"])
                tid, how = complete_from_evidence(
                    ctx.conn,
                    ctx.user_id,
                    title=it.title,
                    asunto_id=asunto_id,
                    source_inbox_ids=it.source_inbox_ids,
                    evidence=it.evidence,
                    when=_message_at(ctx.conn, it.source_inbox_ids),
                    target_id=target_id,
                )
                if how == "created":
                    inserted += 1
                else:
                    deduped += 1
                ent = ctx.trace.entity(
                    "mod_tareas",
                    id=tid,
                    label=f"hecha: {it.title}",
                    status="ok",
                    source_inbox_ids=it.source_inbox_ids,
                )
                ent.log(
                    {
                        "completed": "completada por el propio mensaje de la persona",
                        "already": "ya estaba completada; se sumó la evidencia",
                        "created": "registrada ya completada (no existía como pendiente)",
                    }[how],
                    status="info",
                )
                continue
            due_on = it.due_on
            meta: dict[str, Any] = {}
            if it.asunto_hint:
                meta["asunto_hint"] = it.asunto_hint
            if from_doc and day is not None:
                meta["session_day"] = day.isoformat()
            if day is not None and (it.due_on is not None or it.due_kind):
                # Qué mensaje fijó la fecha: contra él compite una lectura posterior que la mueva.
                meta["due_fixed_day"] = day.isoformat()
            # Sin día no hay «próxima sesión» a la que atar la tarea.
            due_kind = it.due_kind if (not from_doc or day is not None) else ""
            if (
                not due_kind
                and due_on is None
                and next_session_msgs.intersection(it.source_inbox_ids)
            ):
                due_kind = "proxima_sesion"
                meta["due_kind_inherited"] = True
            if due_on is None and due_kind == "proxima_sesion":
                meta["due_kind"] = due_kind
                if asunto_id is not None and day is not None:
                    due_on = _next_session(ctx.conn, ctx.user_id, asunto_id, day)
                    if due_on is not None:
                        meta["due_source"] = "proxima_sesion"
            # Dinámicas y consignas de chat sin fecha dicha: se ganan o pierden el mismo día
            # («al primero que…») o valen lo que suele durar una consigna en ESE asunto (el ritmo:
            # la mediana de los plazos que sí vinieron dichos; sin historia, una semana). Sin plazo
            # quedaban pendientes para siempre.
            if due_on is None and due_kind in ("mismo_dia", "una_semana") and day is not None:
                if due_kind == "mismo_dia":
                    due_on = day
                else:
                    plazo = (
                        plazo_tipico(ctx.conn, ctx.user_id, asunto_id).plazo_o_default
                        if asunto_id is not None
                        else UNA_SEMANA_DIAS
                    )
                    due_on = day + timedelta(days=plazo)
                    meta["plazo_inferido_dias"] = plazo
                meta["due_kind"] = due_kind
                meta["due_source"] = due_kind
            if it.points is not None:
                meta["puntos"] = it.points
                meta["dinamica"] = True
            if it.kind == "recurso":
                # Un enlace/archivo suelto: se pega al proyecto o a la tarea que nombra. NUNCA crea
                # un proyecto por sí solo (un link no es un proyecto).
                target = _resource_target(ctx.conn, ctx.user_id, it, asunto_id, parents)
                if target is None:
                    _log.info(
                        "tareas.recurso.sin_destino",
                        user_id=ctx.user_id,
                        title=it.title[:80],
                        hint=it.parent_hint[:80],
                        inbox_ids=list(it.source_inbox_ids),
                    )
                    continue
                attach_links(ctx.conn, ctx.user_id, target, it.links)
                merge_provenance(ctx.conn, target, it.source_inbox_ids)
                ent = ctx.trace.entity(
                    "mod_tareas",
                    id=target,
                    label=f"recurso: {it.title}",
                    status="ok",
                    source_inbox_ids=it.source_inbox_ids,
                )
                ent.log(" · ".join(it.links) or "sin URL", status="info")
                continue
            # El proyecto del paso se BUSCA ahora y se CREA solo si el paso resulta nuevo (o si el
            # existente no tenía padre): un paso ya registrado que llega con otro nombre de proyecto
            # («Proyecto en curso») no debe dejar un proyecto vacío.
            parent_id: int | None = None
            parent_key = " ".join(it.parent_hint.lower().split()) if it.parent_hint else ""
            if parent_key:
                parent_id = parents.get(parent_key)
                if parent_id is None:
                    parent_id = find_parent(
                        ctx.conn, ctx.user_id, title=it.parent_hint, asunto_id=asunto_id
                    )
                    if parent_id is not None:
                        parents[parent_key] = parent_id
                meta["parent_hint"] = it.parent_hint

            if it.links:
                meta["links"] = list(it.links)
            # Categoría: la del modelo; con puntos es una dinámica sí o sí. Un aviso de SEGURIDAD
            # sin fecha vale esos días (`SEGURIDAD_DIAS`): verificar un acceso una semana después
            # no sirve, y sin plazo quedaría pendiente para siempre.
            category = "dinamica" if it.points is not None else it.category
            if category == "seguridad" and due_on is None and day is not None:
                due_on = day + timedelta(days=SEGURIDAD_DIAS)
                meta["due_source"] = "seguridad"
            # Lo que la persona ya marcó HECHO cubre lo que se le pidió hasta ese momento: misma
            # consigna repetida (otra foto del mismo slide) no vuelve a ser un pendiente. Título
            # igual → directo; parecido → juez (codex).
            cover = find_completed_cover(
                ctx.conn, ctx.user_id, title=it.title, asunto_id=asunto_id, message_day=day
            )
            if cover is not None:
                same = cover.exact
                if not same:
                    llm = judge.get()
                    same = (
                        await self._judge_cover(ctx, llm, cover, it, day)
                        if llm is not None
                        else False
                    )
                if same:
                    deduped += 1
                    done_id = int(cover.tarea["id"])
                    merge_provenance(ctx.conn, done_id, it.source_inbox_ids)
                    if it.links:
                        attach_links(ctx.conn, ctx.user_id, done_id, it.links)
                    ent = ctx.trace.entity(
                        "mod_tareas",
                        id=done_id,
                        label=f"{it.title} · ya hecha",
                        status="ok",
                        source_inbox_ids=it.source_inbox_ids,
                    )
                    ent.log(
                        "cubierta por una tarea ya completada" + ("" if cover.exact else " (juez)"),
                        status="info",
                    )
                    continue
            # ¿Ya existe? Primero la regla barata (procedencia, exacto, difuso, tokens); si no
            # decide, la banda media va al juez: la misma obligación llega por varias vías (nota de
            # clase, chat del grupo, diapositiva, correo) con otras palabras, detalles nuevos o el
            # plazo movido, y NO debe nacer dos veces ni perder lo nuevo.
            dup = find_duplicate(
                ctx.conn,
                ctx.user_id,
                title=it.title,
                asunto_id=asunto_id,
                due_on=due_on,
                source_inbox_ids=it.source_inbox_ids,
                parent_id=parent_id,
                message_day=day,
                explicit_due=it.due_on is not None,
            )
            if dup is None:
                for cand in find_similar_candidates(
                    ctx.conn,
                    ctx.user_id,
                    title=it.title,
                    asunto_id=asunto_id,
                    due_on=due_on,
                    source_inbox_ids=it.source_inbox_ids,
                    parent_id=parent_id,
                    fixed_day=day,
                ):
                    llm = judge.get()
                    if llm is None:
                        break
                    if await self._judge_pending(ctx, llm, cand, it, day, due_on):
                        dup = Duplicate(cand.tarea, "juez", cand.score)
                        break
            if dup is not None:
                deduped += 1
                tarea_id = int(dup.tarea["id"])
                ent = ctx.trace.entity(
                    "mod_tareas",
                    id=tarea_id,
                    label=it.title if due_on is None else f"{it.title} · {due_on.isoformat()}",
                    status="ok",
                    source_inbox_ids=it.source_inbox_ids,
                )
                if dup.tarea.get("status") == "cancelada":
                    # Descartada por la persona («no es tarea»): no se vuelve a crear ni se toca.
                    ent.log("descartada por la persona; no se vuelve a crear", status="info")
                    continue
                if dup.tarea.get("parent_id") is None:
                    parent_id = self._ensure_parent(
                        ctx, it, asunto_id, parent_id, parents, hint_counts
                    )
                changes = enrich(
                    ctx.conn,
                    ctx.user_id,
                    tarea_id,
                    title=it.title,
                    description=it.description,
                    evidence=it.evidence,
                    due_on=due_on,
                    message_day=day,
                    fixed_day=_fixed_day_of(ctx.conn, dup.tarea),
                    priority=it.priority,
                    links=it.links,
                    source_inbox_ids=it.source_inbox_ids,
                    parent=parent_id,
                )
                ent.log(
                    f"ya existía (dedup por {dup.matched_by})"
                    + (f"; se sumó: {', '.join(changes)}" if changes else "; sin novedades"),
                    status="info",
                )
                continue
            parent_id = self._ensure_parent(ctx, it, asunto_id, parent_id, parents, hint_counts)
            row = add(
                ctx.conn,
                ctx.user_id,
                title=it.title,
                description=it.description,
                due_on=due_on,
                priority=it.priority,
                asunto=asunto_id,
                metadata=meta or None,
                source_inbox_ids=it.source_inbox_ids,
                evidence=it.evidence,
                origin="extraction",
                parent=parent_id,
                category=category,
                force=True,  # la búsqueda de duplicados ya se hizo arriba (incluido el juez)
            )
            tarea_id = int(row["id"])
            inserted += 1
            label = it.title if due_on is None else f"{it.title} · {due_on.isoformat()}"
            ctx.trace.entity(
                "mod_tareas",
                id=tarea_id,
                label=label,
                status="ok",
                source_inbox_ids=it.source_inbox_ids,
            )
        _log.info(
            "tareas.extract.persisted",
            user_id=ctx.user_id,
            inserted=inserted,
            deduped=deduped,
            inbox_ids=list(ctx.inbox_ids),
        )
        # Mantenimiento incremental: lo que ya paso de fecha y estaba atado a una sesion o a un
        # curso se cierra como vencida en la misma pasada (sin LLM).
        expire_overdue(ctx.conn, ctx.user_id)
        await judge.close()
        return len(tareas)

    async def _judge_cover(
        self,
        ctx: ModuleContext,
        llm: LLMClient,
        cover: Any,
        it: TareaItem,
        day: date | None,
    ) -> bool:
        """Juez atómico: ¿la candidata es la misma tarea ya completada? Registra la llamada en
        `llm_calls` (`tareas_judge`). Sin juez (error del proveedor) NO se cubre: mejor un
        pendiente de más que esconder una tarea nueva."""
        inbox_id = int(it.source_inbox_ids[0]) if it.source_inbox_ids else None
        try:
            verdict, result = await tareas_judge.judge_same_task(
                llm,
                done=cover.tarea,
                new_title=it.title,
                new_evidence=it.evidence,
                new_day=day,
            )
        except LLMQuotaError:
            raise
        except LLMError as exc:
            _log.warning(
                "tareas.judge.llm_error", user_id=ctx.user_id, title=it.title[:80], error=str(exc)
            )
            return False
        same = verdict.same and verdict.confidence >= tareas_judge.MIN_CONFIDENCE
        record_llm_call(
            user_id=ctx.user_id,
            purpose=tareas_judge.PURPOSE,
            model=result.model,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            status="ok",
            inbox_id=inbox_id,
            cache_hit_tokens=result.usage.cache_hit_tokens,
            metadata={
                "done_tarea_id": int(cover.tarea["id"]),
                "done_title": cover.tarea.get("title"),
                "new_title": it.title,
                "score": cover.score,
                "same": verdict.same,
                "confidence": verdict.confidence,
                "applied": same,
                "reason": verdict.reason,
            },
            response_text=result.content,
        )
        _log.info(
            "tareas.judge",
            user_id=ctx.user_id,
            done_tarea_id=int(cover.tarea["id"]),
            new_title=it.title[:80],
            same=verdict.same,
            confidence=verdict.confidence,
            applied=same,
        )
        return same

    @staticmethod
    def _ensure_parent(
        ctx: ModuleContext,
        it: TareaItem,
        asunto_id: int | None,
        parent_id: int | None,
        parents: dict[str, int],
        hint_counts: Mapping[str, int],
    ) -> int | None:
        """Crea el proyecto del `parent_hint` si hace falta (el paso resultó nuevo, o el existente
        no tenía padre) y lo recuerda para el resto de la pasada.

        UN solo paso no hace un proyecto: el modelo pone `parent_hint` también a una tarea suelta
        con encabezado («Homework: …», «Tarea de la semana») y nacía un «proyecto» de un paso que
        el usuario veía repetido y vacío. El proyecto nace cuando el nombre lo comparten al menos
        dos pasos (en esta pasada o ya registrados sueltos con ese `parent_hint` en el asunto), o
        cuando ya existe. Mientras tanto el paso queda suelto con su `metadata.parent_hint`, y se
        cuelga solo cuando el proyecto aparece."""
        if not it.parent_hint or parent_id is not None:
            return parent_id
        key = " ".join(it.parent_hint.lower().split())
        siblings = loose_steps_with_hint(
            ctx.conn, ctx.user_id, hint=it.parent_hint, asunto_id=asunto_id
        )
        if hint_counts.get(key, 0) + len(siblings) < 2:
            _log.info(
                "tareas.parent_deferred",
                user_id=ctx.user_id,
                hint=it.parent_hint[:80],
                title=it.title[:80],
            )
            return None
        created = find_or_create_parent(
            ctx.conn,
            ctx.user_id,
            title=it.parent_hint,
            asunto_id=asunto_id,
            source_inbox_ids=it.source_inbox_ids,
        )
        parents[key] = created
        for sid in siblings:
            set_parent(ctx.conn, ctx.user_id, sid, created)
        return created

    async def _judge_pending(
        self,
        ctx: ModuleContext,
        llm: LLMClient,
        cand: Any,
        it: TareaItem,
        day: date | None,
        due_on: date | None,
    ) -> bool:
        """Juez atómico: ¿la candidata es la MISMA tarea pendiente ya registrada (otra fuente, otras
        palabras, detalles nuevos, plazo movido) o una distinta? Sin juez (error del proveedor) es
        distinta: mejor un pendiente de más que fundir dos tareas."""
        inbox_id = int(it.source_inbox_ids[0]) if it.source_inbox_ids else None
        try:
            verdict, result = await tareas_judge.judge_same_pending(
                llm,
                existing=cand.tarea,
                fixed_day=_fixed_day_of(ctx.conn, cand.tarea),
                new_title=it.title,
                new_description=it.description,
                new_evidence=it.evidence,
                new_day=day,
                new_due=due_on,
            )
        except LLMQuotaError:
            raise
        except LLMError as exc:
            _log.warning(
                "tareas.judge.llm_error", user_id=ctx.user_id, title=it.title[:80], error=str(exc)
            )
            return False
        same = verdict.same and verdict.confidence >= tareas_judge.MIN_CONFIDENCE
        record_llm_call(
            user_id=ctx.user_id,
            purpose=tareas_judge.PURPOSE,
            model=result.model,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            status="ok",
            inbox_id=inbox_id,
            cache_hit_tokens=result.usage.cache_hit_tokens,
            metadata={
                "kind": "pending",
                "existing_tarea_id": int(cand.tarea["id"]),
                "existing_title": cand.tarea.get("title"),
                "new_title": it.title,
                "score": cand.score,
                "same_message": cand.same_message,
                "same_due": cand.same_due,
                "semantic": cand.semantic,
                "plan_step": cand.plan_step,
                "same": verdict.same,
                "confidence": verdict.confidence,
                "applied": same,
                "reason": verdict.reason,
            },
            response_text=result.content,
        )
        _log.info(
            "tareas.judge.pending",
            user_id=ctx.user_id,
            existing_tarea_id=int(cand.tarea["id"]),
            new_title=it.title[:80],
            same=verdict.same,
            confidence=verdict.confidence,
            applied=same,
        )
        return same

    async def health_check(self) -> HealthResult:
        return HealthResult(
            status="healthy", detail="tareas module ready", checked_at=datetime.now(UTC)
        )

    def read_for_inbox(
        self, conn: Connection, user_id: int, inbox_ids: Sequence[int]
    ) -> list[dict[str, Any]]:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT title, description, due_on, priority, status, asunto_id, evidence
                    FROM mod_tareas
                    WHERE user_id = :uid AND CAST(:ids AS BIGINT[]) && source_inbox_ids
                    ORDER BY id
                    """
                ),
                {"uid": user_id, "ids": list(inbox_ids)},
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]

    def forget_inbox(self, conn: Connection, user_id: int, inbox_ids: Sequence[int]) -> int:
        """Re-extracción en limpio: saca esos mensajes de la procedencia y borra solo las tareas
        EXTRAÍDAS que quedan sin ningún mensaje y que el usuario NO tocó. Una completada o
        cancelada (hecha, descartada) es una decisión suya: se conserva con su procedencia, y la
        re-extracción la reconoce por procedencia en vez de crearla de nuevo. Una manual nunca
        tiene procedencia y no se toca."""
        ids = [int(i) for i in inbox_ids]
        if not ids:
            return 0
        emptied = (
            conn.execute(
                text(
                    """
                    UPDATE mod_tareas
                       SET source_inbox_ids = ARRAY(
                           SELECT x FROM unnest(source_inbox_ids) AS x
                            WHERE NOT (x = ANY(CAST(:ids AS BIGINT[])))
                       )
                     WHERE user_id = :uid AND origin = 'extraction'
                       AND status IN ('pendiente', 'vencida')
                       AND CAST(:ids AS BIGINT[]) && source_inbox_ids
                    RETURNING id
                    """
                ),
                {"uid": user_id, "ids": ids},
            )
            .scalars()
            .all()
        )
        if not emptied:
            return 0
        result = conn.execute(
            text(
                "DELETE FROM mod_tareas WHERE user_id = :uid "
                "AND id = ANY(CAST(:eids AS BIGINT[])) AND cardinality(source_inbox_ids) = 0"
            ),
            {"uid": user_id, "eids": [int(e) for e in emptied]},
        )
        return int(result.rowcount or 0)
