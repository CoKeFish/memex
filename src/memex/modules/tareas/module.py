"""`tareas` — registrador DETERMINISTA de pendientes accionables (sin LLM, sin extracción).

Una tarea es algo que HAY QUE HACER: «ejercicios de la página 42 para el jueves» (del curso del
Britano), «cambiar el punto de atención de la EPS» (sin fecha). Se distingue del calendario (cosas
que OCURREN en un momento) y de asuntos (hilos inferidos de los mensajes, con estado y evidencia):
`due_on` es opcional y `asunto_id` opcional la cuelga del asunto al que pertenece.

Contrato del módulo (nivel 2: depende de asuntos; vértice `tarea`; arista `tarea → asunto`
«parte_de»; dedup = clave natural + resolución difusa en el write-path):

- `add`: dedup ANTES de insertar. Clave natural = `memex_norm(title)` igual entre tareas PENDIENTES
  del mismo asunto (o sueltas, si la nueva no trae asunto). Resolución difusa = títulos casi iguales
  (`difflib` ≥ `FUZZY_THRESHOLD`) con fecha límite compatible (igual, o alguna sin fecha). Un
  duplicado NO se inserta: se devuelve la tarea existente con `deduped=True`. `force=True` lo
  saltea («no, es otra distinta»). Una tarea HECHA o CANCELADA no bloquea: repetir una tarea
  cumplida es una tarea nueva.
- `list`: las vencidas primero, luego por fecha límite, las SIN fecha al final; dentro, prioridad.
- `set_status` / `update` / `delete`: mantenimiento del dato; el grafo se toca solo vía
  `graph_writer` (tejedores de `relations.deterministic`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.config import settings
from memex.core.localtime import LOCAL_TZ
from memex.logging import get_logger
from memex.modules.identidades.normalize import normalize_match
from memex.relations.deterministic import weave_event, weave_tarea
from memex.relations.edges import TAREA_SLUG, Ref
from memex.relations.graph_writer import add_vertex, delete_vertex, update_vertex

_log = get_logger("memex.modules.tareas")

STATUS_PENDIENTE = "pendiente"
STATUS_COMPLETADA = "completada"
STATUS_CANCELADA = "cancelada"
#: Cierre automático: pasó la fecha de una tarea atada a una sesión (o de un curso/programa) y
#: ya no aplica. No dice que se hizo (completada) ni que se decidió no hacerla (cancelada).
STATUS_VENCIDA = "vencida"
STATUSES: tuple[str, ...] = (STATUS_PENDIENTE, STATUS_COMPLETADA, STATUS_CANCELADA, STATUS_VENCIDA)
#: Tipos de asunto cuyas tareas con fecha vencen solas: la fecha ES la clase/sesión (curso) o el
#: evento/hackatón ya ocurrió.
EXPIRING_ASUNTO_KINDS: tuple[str, ...] = ("programa", "hackathon", "evento")
#: En un evento o hackatón, una tarea SIN fecha extraída de un mensaje de hace más de N días
#: nació muerta (el evento pasó). No en un curso ni en un trámite, que duran semanas o meses.
STALE_UNDATED_KINDS: tuple[str, ...] = ("hackathon", "evento")
STALE_UNDATED_DAYS = 30
#: Un PROYECTO (tarea con pasos) de un curso, hackatón o evento no tiene fecha propia: su entrega es
#: la mayor de sus pasos. Vence cuando TODOS sus pasos tienen fecha y la última pasó hace más que
#: la GRACIA del proyecto (`ritmo.gracia_proyecto`: el plazo típico del asunto si sus pasos
#: llegaron un solo día, el doble de su cadencia si llegaron en varios), y sus pasos pendientes
#: vencen con él. Un proyecto con un paso sin fecha sigue vivo, y un paso nuevo lo revive.

#: Prioridad: nombre ↔ número (1 = más urgente). El CLI habla en nombres; la DB guarda el número.
PRIORITY_BY_NAME: dict[str, int] = {"alta": 1, "normal": 2, "baja": 3}
PRIORITY_NAME: dict[int, str] = {v: k for k, v in PRIORITY_BY_NAME.items()}

#: Categoría: de qué clase de cosa es la tarea, para ordenar y agrupar los pendientes (2026-09-26).
#: `seguridad` = cuentas y accesos (intento de acceso, contraseña, 2FA, dispositivo nuevo): se
#: captura «por si acaso», con prioridad alta y un plazo corto (`extract.SEGURIDAD_DIAS`).
#: `entrega` = algo que producir y enviar (trabajo, ejercicio, documento, video, publicación).
#: `tramite` = papeles, formularios, citas o confirmaciones con una institución o servicio.
#: `pago` = pagar, cobrar, renovar. `inscripcion` = registrarse, postularse, anotarse.
#: `dinamica` = retos y dinámicas de un programa (puntos). `otro` = el resto.
CATEGORIES: tuple[str, ...] = (
    "seguridad",
    "entrega",
    "tramite",
    "pago",
    "inscripcion",
    "dinamica",
    "otro",
)
CATEGORY_DEFAULT = "otro"


def category_value(category: str | None) -> str:
    """Normaliza y valida una categoría; vacío/None = `otro`."""
    if category is None or not str(category).strip():
        return CATEGORY_DEFAULT
    key = normalize_match(str(category))
    if key not in CATEGORIES:
        raise ValueError(f"categoría inválida: {category!r}; válidas: {', '.join(CATEGORIES)}")
    return key


#: Similitud mínima (`difflib.SequenceMatcher.ratio` sobre títulos normalizados) para que dos
#: títulos distintos cuenten como la MISMA tarea. 0.85 = «pagar matrícula del Britano» vs «pagar la
#: matrícula del Britano»; no junta «ejercicios página 42» con «ejercicios página 43» (dígitos
#: distintos pesan poco en el ratio, por eso además se exige la misma fecha límite si ambas la
#: tienen).
FUZZY_THRESHOLD = 0.85

#: Segunda vía difusa, por TOKENS: fracción de los tokens del título más corto que aparecen en el
#: otro (igualdad o prefijo: «pag» ~ «pagina»), tras quitar palabras vacías y el nombre del asunto
#: cuando ambas cuelgan del mismo («… del Britano» no distingue nada dentro del asunto Britano).
#: 0.8 junta «hacer los ejercicios de la página 42» con «hacer los ejercicios de la pág. 42 del
#: Britano» (4/4) y NO junta «ejercicios página 42» con «ejercicios página 43» (2/3).
TOKEN_THRESHOLD = 0.8
#: Contención de tokens cuando las dos tareas salen del MISMO mensaje: el prior de «es la misma»
#: es fuerte (tres fotos de la misma diapositiva → «Completar la autoevaluación de desempeño» vs
#: «… (self-assessment)»), así que alcanza con compartir ≥ 60 % de los tokens del título corto y
#: al menos DOS tokens (una sola palabra en común no dice nada).
PROVENANCE_TOKEN_THRESHOLD = 0.6
PROVENANCE_MIN_SHARED_TOKENS = 2
_STOPWORDS = frozenset(
    [
        "de",
        "del",
        "la",
        "el",
        "los",
        "las",
        "para",
        "en",
        "a",
        "y",
        "un",
        "una",
        "al",
        "lo",
        "que",
        "con",
        "por",
        "o",
        "sin",
        "sobre",
        "mi",
        "mis",
        "tu",
        "tus",
        "su",
        "sus",
        "hay",
        "hacer",
        "tengo",
    ]
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")

_COLS = """
    t.id, t.title, t.description, t.due_on, t.priority, t.status, t.asunto_id,
    a.name AS asunto_name, t.done_at, t.metadata, t.event_id, t.created_at, t.updated_at,
    t.source_inbox_ids, t.origin, t.parent_id, t.category
"""
_FROM = "FROM mod_tareas t LEFT JOIN mod_asuntos a ON a.id = t.asunto_id"


class TareaNotFoundError(LookupError):
    """No existe esa tarea para el user."""


class AsuntoNotFoundError(ValueError):
    """`--asunto` no coincide con ningún asunto vivo (por id ni por nombre/alias)."""


@dataclass(frozen=True)
class Duplicate:
    tarea: dict[str, Any]
    matched_by: str  #: 'exact' | 'fuzzy'
    similarity: float


def _today() -> date:
    return datetime.now(LOCAL_TZ).date()


def _public(row: Mapping[Any, Any], *, today: date | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {str(k): v for k, v in dict(row).items()}
    due = d.get("due_on")
    d["due_on"] = due.isoformat() if isinstance(due, date) else None
    d["priority"] = PRIORITY_NAME.get(int(d["priority"]), "normal")
    d["overdue"] = bool(
        d["status"] == STATUS_PENDIENTE and isinstance(due, date) and due < (today or _today())
    )
    return d


def priority_value(priority: str | int | None) -> int:
    if priority is None or priority == "":
        return PRIORITY_BY_NAME["normal"]
    if isinstance(priority, int):
        if priority not in PRIORITY_NAME:
            raise ValueError(f"prioridad inválida: {priority}; válidas: 1 (alta), 2, 3 (baja)")
        return priority
    key = normalize_match(priority)
    if key not in PRIORITY_BY_NAME:
        raise ValueError(f"prioridad inválida: {priority!r}; válidas: alta, normal, baja")
    return PRIORITY_BY_NAME[key]


# --- asuntos ---------------------------------------------------------------------------- #


def resolve_asunto(conn: Connection, user_id: int, ref: str | int | None) -> int | None:
    """`--asunto <id|texto>` → id de un asunto VIVO (no fundido). Por id directo; por texto, primero
    igualdad normalizada de nombre/alias (`memex_norm`), luego «contiene» (el más reciente gana).
    `None`/'' → sin asunto. Lanza `AsuntoNotFoundError` si no hay coincidencia."""
    if ref is None or (isinstance(ref, str) and not ref.strip()):
        return None
    if isinstance(ref, int) or str(ref).strip().isdigit():
        aid = int(ref)
        row = conn.execute(
            text(
                "SELECT id FROM mod_asuntos "
                "WHERE user_id = :u AND id = :i AND merged_into_id IS NULL"
            ),
            {"u": user_id, "i": aid},
        ).first()
        if row is None:
            raise AsuntoNotFoundError(f"no existe el asunto #{aid}")
        return aid
    q = str(ref).strip()
    row = conn.execute(
        text(
            """
            SELECT id,
                   (memex_norm(name) = memex_norm(:q)
                    OR EXISTS (SELECT 1 FROM unnest(aliases) al
                               WHERE memex_norm(al) = memex_norm(:q))) AS exact
            FROM mod_asuntos
            WHERE user_id = :u AND merged_into_id IS NULL
              AND (name ILIKE :like
                   OR EXISTS (SELECT 1 FROM unnest(aliases) al WHERE al ILIKE :like))
            ORDER BY exact DESC, (status = 'abierto') DESC, last_signal_at DESC NULLS LAST, id DESC
            LIMIT 1
            """
        ),
        {"u": user_id, "q": q, "like": f"%{q}%"},
    ).first()
    if row is None:
        raise AsuntoNotFoundError(f"ningún asunto coincide con {q!r} (memex asuntos list --q ...)")
    return int(row[0])


# --- dedup ------------------------------------------------------------------------------ #


def _tokens(title: str, *, drop: frozenset[str] = frozenset()) -> frozenset[str]:
    return frozenset(
        t
        for t in _TOKEN_RE.findall(normalize_match(title))
        if t not in _STOPWORDS and t not in drop
    )


def _token_match(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 3 and long_.startswith(short)


def token_containment(a: frozenset[str], b: frozenset[str]) -> float:
    """Fracción de los tokens del conjunto más chico con pareja (igual o prefijo) en el otro."""
    if not a or not b:
        return 0.0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    hits = sum(1 for t in short if any(_token_match(t, u) for u in long_))
    return hits / len(short)


#: `metadata.discard_reason` de una tarea que el usuario DESCARTÓ («no es una tarea»): queda
#: cancelada, bloquea que la extracción la vuelva a crear y se registra como feedback del mensaje.
DISCARD_NOT_A_TASK = "no_es_tarea"


def _same_task_by_provenance(
    conn: Connection, user_id: int, *, title: str, source_inbox_ids: Sequence[int]
) -> Duplicate | None:
    """Re-leer el MISMO mensaje no crea otra tarea, esté como esté la anterior (completada,
    cancelada, vencida): misma procedencia + título igual o parecido = la misma tarea."""
    if not source_inbox_ids:
        return None
    rows = (
        conn.execute(
            text(
                f"""
                SELECT {_COLS}, (memex_norm(t.title) = memex_norm(:title)) AS exact
                {_FROM}
                WHERE t.user_id = :u AND t.source_inbox_ids && CAST(:ids AS BIGINT[])
                  AND COALESCE((t.metadata->>'is_parent')::boolean, false) IS NOT TRUE
                  -- una copia descartada como DUPLICADA no absorbe a la real
                  AND COALESCE(t.metadata->>'discard_reason', '') <> 'duplicada'
                ORDER BY exact DESC, t.id
                """
            ),
            {"u": user_id, "title": title, "ids": list(source_inbox_ids)},
        )
        .mappings()
        .all()
    )
    wanted = normalize_match(title)
    wanted_tokens = _tokens(title)
    for r in rows:
        if r["exact"]:
            row = dict(r)
            row.pop("exact", None)
            return Duplicate(_public(row), "exact", 1.0)
    for r in rows:
        other = str(r["title"])
        ratio = SequenceMatcher(None, wanted, normalize_match(other)).ratio()
        if ratio >= FUZZY_THRESHOLD:
            row = dict(r)
            row.pop("exact", None)
            return Duplicate(_public(row), "fuzzy", round(ratio, 3))
        other_tokens = _tokens(other)
        cont = token_containment(wanted_tokens, other_tokens)
        shared = len(wanted_tokens & other_tokens)
        if cont >= PROVENANCE_TOKEN_THRESHOLD and shared >= PROVENANCE_MIN_SHARED_TOKENS:
            row = dict(r)
            row.pop("exact", None)
            return Duplicate(_public(row), "tokens", round(cont, 3))
    return None


def find_duplicate(
    conn: Connection,
    user_id: int,
    *,
    title: str,
    asunto_id: int | None,
    due_on: date | None,
    source_inbox_ids: Sequence[int] = (),
    parent_id: int | None = None,
    message_day: date | None = None,
    explicit_due: bool = False,
) -> Duplicate | None:
    """La tarea que ya representa a esta. Primero por PROCEDENCIA (mismo mensaje, cualquier estado:
    re-extraer no resucita ni duplica). Después entre las VIVAS (pendiente|vencida) y las
    DESCARTADAS por el usuario («no es tarea», que bloquean para siempre): mismo asunto si la nueva
    trae uno; si no, todas las del user. Exacto por `memex_norm`; difuso por similitud + fecha
    límite compatible.

    Regla TEMPORAL (2026-09-26): una tarea cuyo plazo YA VENCIÓ antes del día del mensaje nuevo
    (`message_day`) no es duplicado determinista aunque el título sea idéntico, salvo que el
    mensaje nuevo traiga su propia fecha (`explicit_due`: la misma entrega con el plazo movido,
    una prórroga). La misma consigna genérica («revisar el sitio web y tomar notas») se repite en
    otra sesión con otro material y es otra tarea; la lámina de la sesión 6 se fundió en la de la
    sesión 2 por tener las mismas palabras. Esos casos van al juez con las fechas a la vista
    (`find_similar_candidates`). Es el criterio de *temporal record linkage* (Li, Dong, Maurino,
    Srivastava 2011): el mismo valor en dos momentos lejanos puede ser dos entidades."""
    by_prov = _same_task_by_provenance(
        conn, user_id, title=title, source_inbox_ids=source_inbox_ids
    )
    if by_prov is not None:
        return by_prov

    def expired_before(r: Mapping[Any, Any]) -> bool:
        return (
            message_day is not None
            and not explicit_due
            and r["due_on"] is not None
            and r["due_on"] < message_day
        )

    scope = "AND t.asunto_id = :aid" if asunto_id is not None else ""
    params: dict[str, Any] = {"u": user_id, "title": title, "discard": DISCARD_NOT_A_TASK}
    if asunto_id is not None:
        params["aid"] = asunto_id
    rows = (
        conn.execute(
            text(
                f"""
                SELECT {_COLS}, (memex_norm(t.title) = memex_norm(:title)) AS exact
                {_FROM}
                WHERE t.user_id = :u
                  AND (t.status IN ('pendiente', 'vencida')
                       OR (t.status = 'cancelada' AND t.metadata->>'discard_reason' = :discard))
                  -- solo «no es tarea» bloquea por título; una «duplicada» no
                  AND COALESCE((t.metadata->>'is_parent')::boolean, false) IS NOT TRUE
                  {scope}
                ORDER BY exact DESC, t.id
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    for r in rows:
        if r["exact"] and not expired_before(r):
            row = dict(r)
            row.pop("exact", None)
            return Duplicate(_public(row), "exact", 1.0)
    wanted = normalize_match(title)
    asunto_tokens: frozenset[str] = frozenset()
    if asunto_id is not None:
        name = conn.execute(
            text("SELECT name FROM mod_asuntos WHERE id = :a"), {"a": asunto_id}
        ).scalar()
        asunto_tokens = _tokens(str(name or ""))
    wanted_tokens = _tokens(title, drop=asunto_tokens)
    best: Duplicate | None = None
    for r in rows:
        existing_due = r["due_on"]
        if due_on is not None and existing_due is not None and existing_due != due_on:
            continue  # dos tareas parecidas con fechas límite distintas son dos tareas
        if expired_before(r):
            continue  # vencida antes de que llegara este pedido: que decida el juez
        other = str(r["title"])
        ratio = SequenceMatcher(None, wanted, normalize_match(other)).ratio()
        matched_by, score = "fuzzy", ratio
        if ratio < FUZZY_THRESHOLD:
            drop = asunto_tokens if r["asunto_id"] == asunto_id else frozenset()
            other_tokens = _tokens(other, drop=drop)
            cont = token_containment(wanted_tokens, other_tokens)
            # Dos pasos del MISMO proyecto que comparten la mayoría de las palabras son el
            # mismo paso (la misma diapositiva en dos mensajes): umbral de hermanos.
            siblings = parent_id is not None and r.get("parent_id") == parent_id
            shared = len(wanted_tokens & other_tokens)
            if cont >= TOKEN_THRESHOLD or (
                siblings
                and cont >= PROVENANCE_TOKEN_THRESHOLD
                and shared >= PROVENANCE_MIN_SHARED_TOKENS
            ):
                matched_by, score = "tokens", cont
            else:
                continue
        if best is None or score > best.similarity:
            row = dict(r)
            row.pop("exact", None)
            best = Duplicate(_public(row), matched_by, round(score, 3))
    return best


#: Banda MEDIA de parecido: no alcanza para que `find_duplicate` decida solo, pero sí para
#: sospechar que la candidata es una tarea ya registrada leída en OTRA fuente (chat, nota de clase,
#: diapositiva, correo) y preguntarle al juez. Comparten ≥ 2 tokens de contenido y ≥ 40 % del
#: título corto; o vienen del MISMO mensaje con ≥ 1 token y ≥ 30 % (dos redacciones de una
#: consigna en un texto largo, p. ej. resumen + transcripción); o tienen la MISMA fecha límite
#: con ≥ 1 token.
SIMILAR_TOKEN_THRESHOLD = 0.4
SIMILAR_MIN_SHARED_TOKENS = 2
SIMILAR_SAME_MESSAGE_THRESHOLD = 0.3
#: Con 2, una tarea vieja parecida («ver el video y tomar notas») tapaba a la gemela real: el
#: juez es barato comparado con un duplicado que el dueño tiene que descubrir.
SIMILAR_MAX_CANDIDATES = 4
#: Largo máximo que acumulan descripción y evidencia al enriquecer (varias fuentes).
ENRICH_MAX_CHARS = 2000
ENRICH_MAX_ALIASES = 5


@dataclass(frozen=True)
class SimilarCandidate:
    tarea: dict[str, Any]
    score: float
    same_message: bool
    same_due: bool
    #: Pedida en la misma sesión (mismo `due_fixed_day`/`session_day`): la gemela típica.
    same_session: bool = False
    #: Es un PASO de un proyecto del asunto (el plan): llega al juez aunque no comparta palabras
    #: con la candidata («reclamar el pasaporte» vs «recoger y revisar el pasaporte»).
    plan_step: bool = False
    #: Vecina por SIGNIFICADO (bloqueo semántico, `memex.semantic`): `score` es el coseno.
    semantic: bool = False
    #: Vive en OTRO asunto de la misma familia (relacionado por `parte_de`/`requisito_de`/
    #: `continua`): la misma obligación vista desde el programa y desde la beca que es parte de él.
    other_asunto: bool = False


#: Módulo dueño de los vectores de tareas en `semantic_vectors`.
SEMANTIC_SLUG = "tareas"


def family_of(conn: Connection, user_id: int, asunto_id: int | None) -> list[int]:
    """El asunto y los asuntos VIVOS relacionados con él (un salto por `mod_asuntos_relations`,
    en cualquier dirección y de cualquier tipo). Es el alcance del dedup de tareas: la misma
    obligación llega por el chat del programa (asunto padre) y por el correo de la beca que es
    parte de él (asunto hijo), y antes cada asunto se comparaba solo consigo mismo (2026-09-25:
    «grabar el video de 60 s» nació tres veces en dos asuntos relacionados). Sin asunto: []."""
    if asunto_id is None:
        return []
    rows = conn.execute(
        text(
            """
            SELECT CASE WHEN r.src_asunto_id = :a THEN r.dst_asunto_id ELSE r.src_asunto_id END
              FROM mod_asuntos_relations r
              JOIN mod_asuntos o
                ON o.id = CASE WHEN r.src_asunto_id = :a THEN r.dst_asunto_id
                               ELSE r.src_asunto_id END
             WHERE r.user_id = :u AND r.status = 'active'
               AND (r.src_asunto_id = :a OR r.dst_asunto_id = :a)
               AND o.merged_into_id IS NULL
            """
        ),
        {"u": user_id, "a": asunto_id},
    ).scalars()
    others = sorted({int(x) for x in rows} - {asunto_id})
    return [asunto_id, *others]


def _family_tokens(conn: Connection, user_id: int, family: Sequence[int]) -> frozenset[str]:
    """Palabras de los nombres de los asuntos de la familia: no cuentan como parecido."""
    if not family:
        return frozenset()
    names = conn.execute(
        text("SELECT name FROM mod_asuntos WHERE user_id = :u AND id = ANY(:ids)"),
        {"u": user_id, "ids": list(family)},
    ).scalars()
    out: frozenset[str] = frozenset()
    for n in names:
        out = out | _tokens(str(n or ""))
    return out


_NUMBERS_RE = re.compile(r"\d+")


def _numbers(title: str) -> set[str]:
    return set(_NUMBERS_RE.findall(title))


def different_numbers(a: str, b: str) -> bool:
    """Veto determinista al bloqueo semántico: los embeddings no distinguen «página 42» de
    «página 43» (brecha numérica conocida). Dos títulos con números y sin ninguno en común no son
    candidatos por significado; la regla léxica sigue pudiendo proponerlos."""
    na, nb = _numbers(a), _numbers(b)
    return bool(na) and bool(nb) and na.isdisjoint(nb)


def embed_tarea(conn: Connection, user_id: int, tarea_id: int, title: str) -> None:
    """Guarda (o refresca) el vector del título. Best-effort: el bloqueo semántico nunca puede
    hacer fallar un alta ni una extracción."""
    from memex.semantic.store import upsert_vectors

    try:
        upsert_vectors(conn, user_id, SEMANTIC_SLUG, [(tarea_id, title)])
    except Exception as exc:  # pragma: no cover - defensivo: nunca rompe el alta
        _log.warning("tareas.semantic.embed_failed", tarea_id=tarea_id, error=str(exc)[:200])


def semantic_neighbors(
    conn: Connection,
    user_id: int,
    title: str,
    rows: Sequence[Mapping[Any, Any]],
    *,
    k: int | None = None,
    min_score: float | None = None,
) -> dict[int, float]:
    """id → coseno de las filas (ya en alcance) que se parecen por significado a `title`: top-k
    con piso (`settings.semantic_top_k` / `semantic_min_score` salvo que se pasen), sin números
    incompatibles. `min_score=0.0` y `k=len(rows)` = ordenar TODO el alcance por parecido."""
    from memex.semantic.store import neighbors

    scope = {int(r["id"]): str(r["title"]) for r in rows}
    if not scope:
        return {}
    try:
        found = neighbors(
            conn,
            user_id,
            SEMANTIC_SLUG,
            title,
            scope_ids=list(scope),
            k=settings.semantic_top_k if k is None else k,
            min_score=settings.semantic_min_score if min_score is None else min_score,
        )
    except Exception as exc:  # pragma: no cover - defensivo
        _log.warning("tareas.semantic.neighbors_failed", error=str(exc)[:200])
        return {}
    return {
        n.ref_id: n.score for n in found if not different_numbers(title, scope.get(n.ref_id, ""))
    }


def find_similar_candidates(
    conn: Connection,
    user_id: int,
    *,
    title: str,
    asunto_id: int | None,
    due_on: date | None,
    source_inbox_ids: Sequence[int] = (),
    parent_id: int | None = None,
    fixed_day: date | None = None,
    exclude_ids: Sequence[int] = (),
) -> list[SimilarCandidate]:
    """Tareas VIVAS (pendiente|vencida) del mismo asunto (o del user si la nueva no trae asunto) que
    se parecen lo suficiente para SOSPECHAR que son la misma que la candidata, sin que
    `find_duplicate` lo haya resuelto: las decide el juez. La fecha NO filtra (un mensaje posterior
    puede mover el plazo); sí ordena: mismo mensaje primero, después por parecido. Tope
    `SIMILAR_MAX_CANDIDATES` para acotar las llamadas al juez.

    Los PASOS de los proyectos del asunto (el plan que la persona ya tiene) entran SIEMPRE como
    candidatas de una tarea suelta, aunque no compartan ni una palabra: el parecido por tokens
    quita el nombre del asunto («pasaporte») y no conoce sinónimos («reclamar» / «recoger»), y
    lo que llega por un mensaje suele ser uno de los pasos ya planeados. Van después de las
    parecidas y dentro del mismo tope.

    El alcance es la FAMILIA del asunto (`family_of`: él y los relacionados), no solo el asunto:
    las gemelas de otro asunto de la familia llegan marcadas `other_asunto` y detrás de las del
    propio asunto."""
    family = family_of(conn, user_id, asunto_id)
    scope = "AND t.asunto_id = ANY(:aids)" if family else ""
    params: dict[str, Any] = {"u": user_id}
    if family:
        params["aids"] = family
    rows = (
        conn.execute(
            text(
                f"""
                SELECT {_COLS} {_FROM}
                WHERE t.user_id = :u AND t.status IN ('pendiente', 'vencida')
                  AND COALESCE((t.metadata->>'is_parent')::boolean, false) IS NOT TRUE
                  {scope}
                ORDER BY t.id
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    asunto_tokens = _family_tokens(conn, user_id, family)
    wanted_tokens = _tokens(title, drop=asunto_tokens)
    mine = set(int(i) for i in source_inbox_ids)
    skip = {int(i) for i in exclude_ids}
    out: list[SimilarCandidate] = []
    seen: set[int] = set()

    def foreign(r: Mapping[Any, Any]) -> bool:
        return asunto_id is not None and r["asunto_id"] is not None and r["asunto_id"] != asunto_id

    for r in rows:
        if int(r["id"]) in skip or (parent_id is not None and int(r["id"]) == parent_id):
            continue
        other_tokens = _tokens(str(r["title"]), drop=asunto_tokens)
        shared = len(wanted_tokens & other_tokens)
        if shared == 0:
            continue
        cont = token_containment(wanted_tokens, other_tokens)
        same_message = bool(mine.intersection(int(i) for i in (r["source_inbox_ids"] or [])))
        same_due = due_on is not None and r["due_on"] == due_on
        if not (
            (same_message and cont >= SIMILAR_SAME_MESSAGE_THRESHOLD)
            or same_due
            or (cont >= SIMILAR_TOKEN_THRESHOLD and shared >= SIMILAR_MIN_SHARED_TOKENS)
        ):
            continue
        # Cuándo se pidió la existente: su marca de sesión o, si no la tiene (tareas de antes de
        # que existiera la marca), el día de su mensaje más reciente.
        from memex.modules.tareas.extract import _fixed_day_of

        other_fixed = _fixed_day_of(conn, dict(r))
        same_session = fixed_day is not None and other_fixed == fixed_day
        out.append(
            SimilarCandidate(
                _public(dict(r)),
                round(cont, 3),
                same_message,
                same_due,
                same_session,
                other_asunto=foreign(r),
            )
        )
        seen.add(int(r["id"]))
    # mismo mensaje > misma sesión o mismo plazo > propio asunto > parecido > la más reciente.
    # La sesión pesa más que las palabras: una consigna genérica («revisar el sitio web y tomar
    # notas») se repite cada clase, y la gemela real es la pedida el MISMO día o para el mismo
    # plazo aunque se parezca menos, no la de otra sesión con el título calcado (2026-09-26: la
    # lámina de la sesión 6 se comparó primero con la de la sesión 7, título idéntico, y no con
    # la foto del tablero de su propia sesión).
    out.sort(
        key=lambda c: (
            not c.same_message,
            not (c.same_session or c.same_due),
            c.other_asunto,
            -c.score,
            -int(c.tarea["id"]),
        )
    )
    # Vecinas por SIGNIFICADO (bloqueo semántico): las que la regla léxica no vio porque no
    # comparten palabras («reclamar» / «recoger»). Van después de las léxicas, por coseno.
    by_id = {int(r["id"]): r for r in rows}
    sem = semantic_neighbors(
        conn,
        user_id,
        title,
        [
            r
            for r in rows
            if int(r["id"]) not in skip
            and int(r["id"]) not in seen
            and not (parent_id is not None and int(r["id"]) == parent_id)
        ],
    )
    for rid, score in sorted(sem.items(), key=lambda kv: (-kv[1], kv[0])):
        r = by_id[rid]
        same_message = bool(mine.intersection(int(i) for i in (r["source_inbox_ids"] or [])))
        same_due = due_on is not None and r["due_on"] == due_on
        out.append(
            SimilarCandidate(
                _public(dict(r)),
                score,
                same_message,
                same_due,
                semantic=True,
                other_asunto=foreign(r),
            )
        )
        seen.add(rid)
    if parent_id is None and asunto_id is not None:
        # Los pasos del plan del asunto (y de su familia), sin exigir palabras en común: los más
        # parecidos por significado primero (aunque queden bajo el piso), así el tope de
        # candidatas no deja afuera al paso que de verdad es.
        steps = [
            r
            for r in rows
            if int(r["id"]) not in skip and int(r["id"]) not in seen and r["parent_id"] is not None
        ]
        ranked = semantic_neighbors(conn, user_id, title, steps, k=len(steps), min_score=0.0)
        steps.sort(key=lambda r: (-ranked.get(int(r["id"]), -1.0), int(r["id"])))
        for r in steps:
            same_due = due_on is not None and r["due_on"] == due_on
            out.append(
                SimilarCandidate(
                    _public(dict(r)),
                    0.0,
                    False,
                    same_due,
                    plan_step=True,
                    other_asunto=foreign(r),
                )
            )
    return out[:SIMILAR_MAX_CANDIDATES]


def merge_provenance(conn: Connection, tarea_id: int, inbox_ids: Sequence[int]) -> None:
    """Una tarea ya existente citada por otro mensaje suma ese mensaje a su procedencia."""
    if not inbox_ids:
        return
    conn.execute(
        text(
            """
            UPDATE mod_tareas
               SET source_inbox_ids = ARRAY(
                       SELECT DISTINCT x
                         FROM unnest(source_inbox_ids || CAST(:ids AS BIGINT[])) AS x
                       ORDER BY x),
                   updated_at = NOW()
             WHERE id = :t
            """
        ),
        {"t": tarea_id, "ids": list(inbox_ids)},
    )


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and normalize_match(needle) in normalize_match(haystack)


def _append_text(current: str, new: str, *, prefix: str = "", limit: int = ENRICH_MAX_CHARS) -> str:
    """Suma `new` a `current` sin repetir: si ya está contenido no hace nada; si `new` contiene a
    `current` lo reemplaza (es la versión más completa); si no, lo agrega en otra línea."""
    cur, add_ = current.strip(), " ".join(new.split())
    if not add_ or _contains(cur, add_):
        return cur
    if not cur:
        return add_[:limit]
    if _contains(add_, cur):
        return add_[:limit]
    return f"{cur}\n{prefix}{add_}"[:limit]


def enrich(
    conn: Connection,
    user_id: int,
    tarea_id: int,
    *,
    title: str = "",
    description: str = "",
    evidence: str = "",
    due_on: date | None = None,
    message_day: date | None = None,
    fixed_day: date | None = None,
    priority: str | int | None = None,
    links: Sequence[str] = (),
    source_inbox_ids: Sequence[int] = (),
    parent: int | None = None,
) -> list[str]:
    """La MISMA tarea leída en otro mensaje (otra fuente, otra redacción) enriquece la que existe en
    vez de crear otra. Determinista:
    - procedencia y enlaces se suman;
    - el detalle nuevo (`description`) se agrega a la descripción con el día del mensaje, salvo que
      ya esté dicho; si contiene a la anterior, la reemplaza; la evidencia (cita) se acumula;
    - la prioridad sube si la nueva es mayor (nunca baja);
    - la fecha se toma si no había; si había otra y el mensaje nuevo es POSTERIOR al que la fijó
      (`fixed_day`, o `metadata.due_fixed_day`), el plazo cambió: se mueve y queda el historial en
      `metadata.due_history`; una vencida que vuelve a pedirse con fecha no pasada se reabre;
    - el título se conserva (estabilidad); la otra redacción va a `metadata.aliases`;
    - un paso huérfano se cuelga del proyecto si la nueva lectura lo trae.
    Devuelve la lista de cambios aplicados (para la traza)."""
    cur = get(conn, user_id, tarea_id)
    meta: dict[str, Any] = dict(cur.get("metadata") or {})
    changes: list[str] = []
    sets: dict[str, Any] = {}

    merge_provenance(conn, tarea_id, source_inbox_ids)

    prefix = f"({message_day.isoformat()}) " if message_day else ""
    desc = _append_text(str(cur.get("description") or ""), description, prefix=prefix)
    if desc != str(cur.get("description") or "").strip():
        sets["description"] = desc
        changes.append("detalle")
    ev = _append_text(str(cur.get("evidence") or ""), evidence, prefix="| ")
    if ev != str(cur.get("evidence") or "").strip():
        sets["evidence"] = ev

    if priority is not None:
        new_prio = priority_value(priority)
        cur_prio = PRIORITY_BY_NAME.get(str(cur.get("priority")), 2)
        if new_prio < cur_prio:
            sets["priority"] = new_prio
            changes.append(f"prioridad {PRIORITY_NAME[new_prio]}")

    cur_due = date.fromisoformat(cur["due_on"]) if cur.get("due_on") else None
    status = str(cur.get("status"))
    reopen = False
    if due_on is not None and status in (STATUS_PENDIENTE, STATUS_VENCIDA):
        if cur_due is None:
            sets["due_on"] = due_on
            if message_day:
                meta["due_fixed_day"] = message_day.isoformat()
            changes.append(f"fecha {due_on.isoformat()}")
        elif cur_due != due_on:
            raw_fixed = meta.get("due_fixed_day")
            fixed = date.fromisoformat(str(raw_fixed)) if raw_fixed else fixed_day
            if message_day is not None and (fixed is None or message_day > fixed):
                sets["due_on"] = due_on
                meta["due_fixed_day"] = message_day.isoformat()
                hist = list(meta.get("due_history") or [])
                hist.append(
                    {
                        "from": cur_due.isoformat(),
                        "to": due_on.isoformat(),
                        "day": message_day.isoformat(),
                        "inbox_ids": [int(i) for i in source_inbox_ids],
                    }
                )
                meta["due_history"] = hist[-10:]
                changes.append(f"fecha {cur_due.isoformat()} → {due_on.isoformat()}")
                if status == STATUS_VENCIDA and due_on >= _today():
                    reopen = True
                    changes.append("reabierta")

    if title and normalize_match(title) != normalize_match(str(cur.get("title") or "")):
        aliases = [str(a) for a in (meta.get("aliases") or [])]
        if not any(normalize_match(a) == normalize_match(title) for a in aliases):
            meta["aliases"] = [*aliases, " ".join(title.split())][-ENRICH_MAX_ALIASES:]

    if parent is not None and cur.get("parent_id") is None and parent != tarea_id:
        _assert_parent(conn, user_id, parent)
        sets["parent_id"] = parent
        changes.append("paso del proyecto")

    assigns = ", ".join(f"{k} = :{k}" for k in sets)
    conn.execute(
        text(
            "UPDATE mod_tareas SET metadata = CAST(:meta AS JSONB), updated_at = NOW()"
            + (f", {assigns}" if assigns else "")
            + " WHERE user_id = :u AND id = :t"
        ),
        {**sets, "meta": json.dumps(meta), "u": user_id, "t": tarea_id},
    )
    # Después del UPDATE de metadata: reabrir (toca status/done_at) y sumar enlaces (escriben
    # metadata.links sobre lo ya guardado).
    if reopen:
        set_status(conn, user_id, tarea_id, STATUS_PENDIENTE)
    if links:
        n = attach_links(conn, user_id, tarea_id, links)
        if n:
            changes.append(f"enlaces +{n}")
    if changes:
        _log.info("tareas.enriched", user_id=user_id, tarea_id=tarea_id, changes=changes)
    return changes


# --- escritura -------------------------------------------------------------------------- #


def add(
    conn: Connection,
    user_id: int,
    *,
    title: str,
    description: str = "",
    due_on: date | None = None,
    priority: str | int | None = None,
    asunto: str | int | None = None,
    metadata: Mapping[str, Any] | None = None,
    event_id: str | None = None,
    force: bool = False,
    source_inbox_ids: Sequence[int] = (),
    evidence: str = "",
    origin: str = "manual",
    parent: int | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """Crea una tarea (o devuelve la existente si es un duplicado: `deduped=True`, `matched_by`).
    Teje en el acto la arista `tarea → asunto` («parte_de») y, con `event_id`, las
    «mismo_evento». `source_inbox_ids`/`evidence`/`origin='extraction'` los pone el extractor
    (`extract.py`): procedencia para la traza y la re-extracción; una tarea manual va sin ellos.
    `parent`: la tarea es un PASO de ese proyecto (tarea padre); si resulta duplicada de una que
    aún no tenía padre, se le cuelga."""
    if origin not in ("manual", "extraction", "calendar"):
        raise ValueError(f"origin inválido: {origin!r}")
    if parent is not None:
        _assert_parent(conn, user_id, parent)
    clean = " ".join(title.split())
    if not clean:
        raise ValueError("el título de la tarea no puede estar vacío")
    asunto_id = resolve_asunto(conn, user_id, asunto)
    prio = priority_value(priority)
    if not force:
        dup = find_duplicate(
            conn,
            user_id,
            title=clean,
            asunto_id=asunto_id,
            due_on=due_on,
            source_inbox_ids=source_inbox_ids,
            parent_id=parent,
        )
        if dup is not None:
            _log.info(
                "tareas.deduped",
                user_id=user_id,
                tarea_id=int(dup.tarea["id"]),
                matched_by=dup.matched_by,
                similarity=dup.similarity,
                title=clean[:80],
            )
            if (
                parent is not None
                and dup.tarea.get("parent_id") is None
                and int(dup.tarea["id"]) != parent
            ):
                conn.execute(
                    text(
                        "UPDATE mod_tareas SET parent_id = :p, updated_at = NOW() "
                        "WHERE user_id = :u AND id = :i"
                    ),
                    {"p": parent, "u": user_id, "i": int(dup.tarea["id"])},
                )
                dup = Duplicate(
                    get(conn, user_id, int(dup.tarea["id"])), dup.matched_by, dup.similarity
                )
            return {**dup.tarea, "deduped": True, "matched_by": dup.matched_by}
    row = (
        conn.execute(
            text(
                """
                INSERT INTO mod_tareas
                  (user_id, title, description, due_on, priority, asunto_id, metadata, event_id,
                   source_inbox_ids, evidence, origin, parent_id, category)
                VALUES (:u, :title, :description, :due_on, :priority, :asunto_id,
                        CAST(:metadata AS JSONB), :event_id, CAST(:ids AS BIGINT[]), :evidence,
                        :origin, :parent, :category)
                RETURNING id
                """
            ),
            {
                "u": user_id,
                "title": clean,
                "description": description.strip(),
                "due_on": due_on,
                "priority": prio,
                "category": category_value(category),
                "asunto_id": asunto_id,
                "metadata": json.dumps(dict(metadata or {})),
                "event_id": event_id,
                "ids": list(source_inbox_ids),
                "evidence": evidence.strip(),
                "origin": origin,
                "parent": parent,
            },
        )
        .mappings()
        .one()
    )
    tarea_id = int(row["id"])
    add_vertex(conn, user_id, Ref(TAREA_SLUG, tarea_id))
    embed_tarea(conn, user_id, tarea_id, clean)
    if asunto_id is not None:
        weave_tarea(conn, user_id, tarea_id)
    if event_id:
        weave_event(conn, user_id, event_id)
    if parent is not None and (due_on is None or due_on >= _today()):
        revive_parent(conn, user_id, parent)
    _log.info(
        "tareas.added",
        user_id=user_id,
        tarea_id=tarea_id,
        due_on=due_on.isoformat() if due_on else None,
        asunto_id=asunto_id,
        priority=PRIORITY_NAME[prio],
    )
    out = get(conn, user_id, tarea_id)
    return {**out, "deduped": False}


def _assert_parent(
    conn: Connection, user_id: int, parent_id: int, *, child_id: int | None = None
) -> None:
    """El padre existe, es del user, no es la misma tarea y no es a su vez un paso (un nivel)."""
    if child_id is not None and parent_id == child_id:
        raise ValueError("una tarea no puede ser su propio proyecto")
    parent = get(conn, user_id, parent_id)
    if parent.get("parent_id") is not None:
        raise ValueError(
            f"la tarea #{parent_id} ya es un paso de #{parent['parent_id']}: un solo nivel"
        )


def revive_parent(conn: Connection, user_id: int, parent_id: int) -> bool:
    """Un paso NUEVO y vigente revive un proyecto VENCIDO: el proyecto vive mientras le lleguen
    pasos (un curso agrega pasos cada semana; si venció por silencio y vuelve a hablar, sigue).
    Solo se llama al insertar un paso nuevo con fecha vigente o sin fecha; re-leer un mensaje
    viejo (paso deduplicado) no lo revive."""
    res = conn.execute(
        text(
            "UPDATE mod_tareas SET status = :p, updated_at = NOW() "
            "WHERE user_id = :u AND id = :i AND status = :v"
        ),
        {"p": STATUS_PENDIENTE, "v": STATUS_VENCIDA, "u": user_id, "i": parent_id},
    )
    if not res.rowcount:
        return False
    update_vertex(conn, user_id, Ref(TAREA_SLUG, parent_id))
    _log.info("tareas.parent_revived", user_id=user_id, tarea_id=parent_id)
    return True


#: Palabras con las que un mensaje nombra «el proyecto» sin decir cuál: un hint hecho solo de
#: estas, cuando el asunto tiene UN solo proyecto vivo, es ese proyecto (no uno nuevo).
GENERIC_PROJECT_TOKENS = frozenset(
    [
        "proyecto",
        "project",
        "final",
        "curso",
        "entrega",
        "trabajo",
        "actual",
        "grupal",
        "grupo",
        "post",
        "posts",
        "clase",
        "semestre",
        "nivel",
    ]
)


def find_parent(
    conn: Connection,
    user_id: int,
    *,
    title: str,
    asunto_id: int | None,
    exclude_id: int | None = None,
) -> int | None:
    """El PROYECTO (tarea padre) del asunto que se llama así (igualdad normalizada o contención de
    tokens), no cancelado; None si no existe. Solo busca: nunca crea. Un nombre GENÉRICO («el
    proyecto en curso», «proyecto de posts») con un ÚNICO proyecto vivo en el asunto es ese: cada
    fuente lo nombra distinto (lámina, chat, nota) y no por eso hay varios proyectos.
    `exclude_id`: un proyecto que busca a su gemelo no debe encontrarse a sí mismo."""
    clean = " ".join(title.split())
    if not clean:
        return None
    scope = "AND t.asunto_id = :aid" if asunto_id is not None else "AND t.asunto_id IS NULL"
    params: dict[str, Any] = {"u": user_id, "t": clean}
    if asunto_id is not None:
        params["aid"] = asunto_id
    if exclude_id is not None:
        scope += " AND t.id <> :x"
        params["x"] = exclude_id
    rows = conn.execute(
        text(
            f"""
            SELECT t.id, t.title FROM mod_tareas t
             WHERE t.user_id = :u AND t.parent_id IS NULL AND t.status <> 'cancelada'
               AND (t.metadata->>'is_parent')::boolean IS TRUE {scope}
             ORDER BY (memex_norm(t.title) = memex_norm(:t)) DESC, t.id
            """
        ),
        params,
    ).all()
    wanted = _tokens(clean)
    for pid, ptitle in rows:
        if normalize_match(str(ptitle)) == normalize_match(clean):
            return int(pid)
        if token_containment(wanted, _tokens(str(ptitle))) >= TOKEN_THRESHOLD:
            return int(pid)
    if asunto_id is not None and len(rows) == 1 and wanted and wanted <= GENERIC_PROJECT_TOKENS:
        pid, ptitle = rows[0]
        _log.info(
            "tareas.parent_generic",
            user_id=user_id,
            tarea_id=int(pid),
            hint=clean[:80],
            parent=str(ptitle)[:80],
        )
        return int(pid)
    return None


def find_or_create_parent(
    conn: Connection,
    user_id: int,
    *,
    title: str,
    asunto_id: int | None,
    source_inbox_ids: Sequence[int] = (),
) -> int:
    """El PROYECTO (tarea padre) al que pertenece un paso, por nombre dentro del asunto: reusa el
    que ya exista (igualdad normalizada o contención de tokens, no cancelado) o lo crea sin
    fecha. Se crea con `force=True` y los padres quedan FUERA del dedup: su título («Proyecto de
    Canva») se parece al de sus pasos y se confundirían."""
    clean = " ".join(title.split())
    if not clean:
        raise ValueError("el nombre del proyecto no puede estar vacío")
    found = find_parent(conn, user_id, title=clean, asunto_id=asunto_id)
    if found is not None:
        return found
    row = add(
        conn,
        user_id,
        title=clean,
        asunto=asunto_id,
        metadata={"is_parent": True},
        force=True,
        source_inbox_ids=source_inbox_ids,
        evidence="agrupa los pasos del proyecto",
        origin="extraction",
    )
    _log.info("tareas.parent_created", user_id=user_id, tarea_id=int(row["id"]), title=clean[:80])
    return int(row["id"])


def is_parent(tarea: Mapping[str, Any]) -> bool:
    return bool((tarea.get("metadata") or {}).get("is_parent"))


def same_project_name(a: str, b: str) -> bool:
    """Dos nombres de proyecto son el mismo si coinciden normalizados o si las palabras de uno
    están (casi) todas en el otro, en cualquier dirección: «Proyecto de Instagram» ⊂ «Proyecto de
    Instagram en Canva (My Social Life)»."""
    if normalize_match(a) == normalize_match(b):
        return True
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return max(token_containment(ta, tb), token_containment(tb, ta)) >= TOKEN_THRESHOLD


def loose_steps_with_hint(
    conn: Connection, user_id: int, *, hint: str, asunto_id: int | None
) -> list[int]:
    """Tareas PENDIENTES sueltas (sin padre, no proyectos) del asunto cuyo `metadata.parent_hint`
    nombra este mismo proyecto: los pasos que llegaron de a uno antes de que existiera."""
    clean = " ".join(hint.split())
    if not clean:
        return []
    scope = "AND t.asunto_id = :aid" if asunto_id is not None else "AND t.asunto_id IS NULL"
    params: dict[str, Any] = {"u": user_id}
    if asunto_id is not None:
        params["aid"] = asunto_id
    rows = conn.execute(
        text(
            f"""
            SELECT t.id, t.metadata->>'parent_hint' FROM mod_tareas t
             WHERE t.user_id = :u AND t.parent_id IS NULL AND t.status = 'pendiente'
               AND (t.metadata->>'is_parent')::boolean IS NOT TRUE
               AND COALESCE(t.metadata->>'parent_hint', '') <> '' {scope}
             ORDER BY t.id
            """
        ),
        params,
    ).all()
    return [int(tid) for tid, h in rows if same_project_name(clean, str(h))]


def set_parent(conn: Connection, user_id: int, tarea_id: int, parent_id: int) -> None:
    conn.execute(
        text(
            "UPDATE mod_tareas SET parent_id = :p, updated_at = NOW() "
            "WHERE user_id = :u AND id = :t"
        ),
        {"p": parent_id, "u": user_id, "t": tarea_id},
    )
    _log.info("tareas.step_attached", user_id=user_id, tarea_id=tarea_id, parent=parent_id)


def merge_parents(conn: Connection, user_id: int, *, survivor_id: int, absorbed_id: int) -> int:
    """Funde dos PROYECTOS que son el mismo (cada fuente lo nombró distinto): los pasos del
    absorbido pasan al superviviente, se suman procedencia y enlaces, su nombre queda como alias y
    el absorbido se borra (un proyecto no tiene contenido propio). Devuelve pasos movidos."""
    if survivor_id == absorbed_id:
        return 0
    s, a = get(conn, user_id, survivor_id), get(conn, user_id, absorbed_id)
    if not is_parent(s) or not is_parent(a):
        raise ValueError("solo se funden proyectos (tareas padre)")
    moved = conn.execute(
        text(
            "UPDATE mod_tareas SET parent_id = :s, updated_at = NOW() "
            "WHERE user_id = :u AND parent_id = :a"
        ),
        {"s": survivor_id, "u": user_id, "a": absorbed_id},
    ).rowcount
    merge_provenance(conn, survivor_id, [int(i) for i in a.get("source_inbox_ids") or []])
    attach_links(conn, user_id, survivor_id, list((a.get("metadata") or {}).get("links") or []))
    aliases = list((s.get("metadata") or {}).get("aliases") or [])
    if a["title"] not in aliases and a["title"] != s["title"]:
        aliases.append(a["title"])
        conn.execute(
            text(
                "UPDATE mod_tareas SET metadata = metadata || CAST(:m AS JSONB), "
                "updated_at = NOW() WHERE user_id = :u AND id = :t"
            ),
            {"m": json.dumps({"aliases": aliases}), "u": user_id, "t": survivor_id},
        )
    delete(conn, user_id, absorbed_id)
    _log.info(
        "tareas.parents_merged",
        user_id=user_id,
        survivor=survivor_id,
        absorbed=absorbed_id,
        steps=int(moved),
    )
    return int(moved)


def dedup_parents(
    conn: Connection, user_id: int, *, asunto_id: int | None = None, apply: bool = True
) -> list[dict[str, Any]]:
    """Proyectos repetidos dentro de un asunto (mismo nombre o uno contenido en el otro). Nacen
    cuando la extracción corre antes que la asignación (una nota sin chat): el proyecto nace suelto
    y al colgarse del asunto nadie lo comparaba con el que ya había. Sobrevive el que tiene más
    pasos (empate: el más viejo). Devuelve una fila por fusión; con `apply=False` solo las lista."""
    scope = "AND t.asunto_id = :aid" if asunto_id is not None else ""
    params: dict[str, Any] = {"u": user_id}
    if asunto_id is not None:
        params["aid"] = asunto_id
    rows = conn.execute(
        text(
            f"""
            SELECT t.id, t.title, t.asunto_id,
                   (SELECT count(*) FROM mod_tareas c WHERE c.parent_id = t.id) AS steps
              FROM mod_tareas t
             WHERE t.user_id = :u AND t.parent_id IS NULL AND t.status <> 'cancelada'
               AND (t.metadata->>'is_parent')::boolean IS TRUE AND t.asunto_id IS NOT NULL {scope}
             ORDER BY t.asunto_id, steps DESC, t.id
            """
        ),
        params,
    ).all()
    out: list[dict[str, Any]] = []
    survivors: dict[int, list[tuple[int, str]]] = {}
    for pid, title, aid, steps in rows:
        kept = survivors.setdefault(int(aid), [])
        twin = next((s for s in kept if same_project_name(str(title), s[1])), None)
        if twin is None:
            kept.append((int(pid), str(title)))
            continue
        out.append(
            {
                "asunto_id": int(aid),
                "survivor": twin[0],
                "survivor_title": twin[1],
                "absorbed": int(pid),
                "absorbed_title": str(title),
                "steps": int(steps),
            }
        )
        if apply:
            merge_parents(conn, user_id, survivor_id=twin[0], absorbed_id=int(pid))
    return out


def flatten_single_parents(
    conn: Connection, user_id: int, *, asunto_id: int | None = None, apply: bool = True
) -> list[dict[str, Any]]:
    """Un «proyecto» de extracción con UN solo paso y sin enlaces propios no es un proyecto (misma
    regla con la que ya no nacen): el paso queda suelto y recuerda el nombre en
    `metadata.parent_hint`, y el padre se borra. Los proyectos anotados a mano no se tocan."""
    scope = "AND t.asunto_id = :aid" if asunto_id is not None else ""
    params: dict[str, Any] = {"u": user_id}
    if asunto_id is not None:
        params["aid"] = asunto_id
    rows = conn.execute(
        text(
            f"""
            SELECT t.id, t.title, t.asunto_id,
                   (SELECT array_agg(c.id ORDER BY c.id) FROM mod_tareas c WHERE c.parent_id = t.id)
              FROM mod_tareas t
             WHERE t.user_id = :u AND t.parent_id IS NULL AND t.status <> 'cancelada'
               AND t.origin = 'extraction'
               AND (t.metadata->>'is_parent')::boolean IS TRUE
               AND COALESCE(jsonb_array_length(t.metadata->'links'), 0) = 0 {scope}
             ORDER BY t.id
            """
        ),
        params,
    ).all()
    out: list[dict[str, Any]] = []
    for pid, title, aid, steps in rows:
        if not steps or len(steps) != 1:
            continue
        out.append(
            {"asunto_id": aid, "parent": int(pid), "title": str(title), "step": int(steps[0])}
        )
        if not apply:
            continue
        conn.execute(
            text(
                "UPDATE mod_tareas SET parent_id = NULL, "
                "metadata = metadata || CAST(:m AS JSONB), updated_at = NOW() "
                "WHERE user_id = :u AND id = :t"
            ),
            {"m": json.dumps({"parent_hint": str(title)}), "u": user_id, "t": int(steps[0])},
        )
        delete(conn, user_id, int(pid))
        _log.info("tareas.parent_flattened", user_id=user_id, tarea_id=int(pid), step=int(steps[0]))
    return out


def attach_links(conn: Connection, user_id: int, tarea_id: int, links: Sequence[str]) -> int:
    """Suma `links` (URLs) a `metadata.links` de la tarea sin repetir. Devuelve cuántos entraron
    nuevos. Un proyecto acumula así la plantilla, el video, el sitio que sus mensajes fueron
    soltando."""
    clean = [str(u).strip() for u in links if str(u).strip()]
    if not clean:
        return 0
    current = get(conn, user_id, tarea_id)
    have = list((current.get("metadata") or {}).get("links") or [])
    new = [u for u in clean if u not in have]
    if not new:
        return 0
    conn.execute(
        text(
            "UPDATE mod_tareas SET metadata = metadata || CAST(:m AS JSONB), updated_at = NOW() "
            "WHERE user_id = :u AND id = :t"
        ),
        {"m": json.dumps({"links": have + new}), "u": user_id, "t": tarea_id},
    )
    _log.info("tareas.links_attached", user_id=user_id, tarea_id=tarea_id, links=len(new))
    return len(new)


#: Parecido mínimo entre una candidata y una tarea COMPLETADA del mismo asunto para sospechar que
#: es la misma (y preguntarle al juez): comparten ≥ 2 tokens de contenido y ≥ 50 % del título corto.
COVER_TOKEN_THRESHOLD = 0.5
COVER_MIN_SHARED_TOKENS = 2


@dataclass(frozen=True)
class CompletedCover:
    """Una tarea ya COMPLETADA que podría cubrir a la candidata: `exact` si el título coincide
    (no hace falta juez); si no, `score` = contención de tokens y decide el juez."""

    tarea: dict[str, Any]
    exact: bool
    score: float
    #: Cobertura propuesta por SIGNIFICADO (coseno), no por tokens.
    semantic: bool = False


def _asunto_tokens(conn: Connection, asunto_id: int | None) -> frozenset[str]:
    if asunto_id is None:
        return frozenset()
    name = conn.execute(
        text("SELECT name FROM mod_asuntos WHERE id = :a"), {"a": asunto_id}
    ).scalar()
    return _tokens(str(name or ""))


def find_completed_cover(
    conn: Connection,
    user_id: int,
    *,
    title: str,
    asunto_id: int | None,
    message_day: date | None,
) -> CompletedCover | None:
    """Lo que la persona ya marcó HECHO cubre lo que se le pidió hasta ese momento: una tarea
    completada del mismo asunto, cuya fecha de completado no es anterior al día del mensaje de la
    candidata, y de título igual (exacto) o parecido (≥ `COVER_MIN_SHARED_TOKENS` tokens y
    ≥ `COVER_TOKEN_THRESHOLD`). Un mensaje POSTERIOR a la completada es una asignación nueva: no
    se cubre. Sin asunto o sin fecha del mensaje no se asume nada.

    Un PASO completado de un proyecto del asunto (el plan) también es cobertura posible aunque no
    comparta palabras (sinónimos, nombre del asunto quitado): con `score` 0, la decide el juez;
    una parecida por tokens gana sobre él."""
    if asunto_id is None or message_day is None:
        return None
    family = family_of(conn, user_id, asunto_id)
    rows = (
        conn.execute(
            text(
                f"""
                SELECT {_COLS}, (memex_norm(t.title) = memex_norm(:title)) AS exact
                {_FROM}
                WHERE t.user_id = :u AND t.asunto_id = ANY(:aids) AND t.status = 'completada'
                  AND t.done_at IS NOT NULL
                  AND COALESCE((t.metadata->>'is_parent')::boolean, false) IS NOT TRUE
                ORDER BY exact DESC, (t.asunto_id = :aid) DESC, t.done_at DESC
                """
            ),
            {"u": user_id, "aid": asunto_id, "aids": family, "title": title},
        )
        .mappings()
        .all()
    )
    if not rows:
        return None
    drop = _family_tokens(conn, user_id, family)
    wanted = _tokens(title, drop=drop)
    best: CompletedCover | None = None
    steps: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    for r in rows:
        done_at = r["done_at"]
        if not isinstance(done_at, datetime):
            continue
        if done_at.tzinfo is None:
            done_at = done_at.replace(tzinfo=UTC)
        if done_at.astimezone(LOCAL_TZ).date() < message_day:
            continue  # se completó ANTES de que se pidiera esto: es otra asignación
        row = dict(r)
        row.pop("exact", None)
        if r["exact"]:
            return CompletedCover(_public(row), exact=True, score=1.0)
        eligible.append(row)
        other = _tokens(str(r["title"]), drop=drop)
        shared = len(wanted & other)
        cont = token_containment(wanted, other)
        if (
            shared >= COVER_MIN_SHARED_TOKENS
            and cont >= COVER_TOKEN_THRESHOLD
            and (best is None or cont > best.score)
        ):
            best = CompletedCover(_public(row), exact=False, score=round(cont, 3))
        elif r["parent_id"] is not None:
            steps.append(row)
    if best is not None:
        return best
    # Sin parecido por tokens: la completada más cercana por SIGNIFICADO, si la hay.
    sem = semantic_neighbors(conn, user_id, title, eligible)
    if sem:
        rid, score = max(sem.items(), key=lambda kv: (kv[1], -kv[0]))
        row = next(x for x in eligible if int(x["id"]) == rid)
        return CompletedCover(_public(row), exact=False, score=score, semantic=True)
    if not steps:
        return None
    # Un paso del plan: el MÁS parecido por significado aunque quede bajo el piso (el juez
    # decide), no el completado más recientemente. 2026-09-26: «postularse a X» tenía 0,47 con
    # «inscribirte en X y elegir track» y 0,18 con «llenar el formulario de la beca», y al juez
    # llegaba el segundo por ser el último completado. Sin vectores, el más reciente.
    ranked = semantic_neighbors(conn, user_id, title, steps, k=len(steps), min_score=0.0)
    if ranked:
        rid = max(ranked.items(), key=lambda kv: (kv[1], -kv[0]))[0]
        row = next(x for x in steps if int(x["id"]) == rid)
        return CompletedCover(_public(row), exact=False, score=0.0)
    return CompletedCover(_public(steps[0]), exact=False, score=0.0)


def find_task_by_title(
    conn: Connection, user_id: int, *, title: str, asunto_id: int | None, statuses: Sequence[str]
) -> dict[str, Any] | None:
    """La tarea del asunto y su familia (`family_of`; o suelta, si no hay asunto) en `statuses`
    que mejor corresponde al título: exacta, o ≥ 60 % de tokens y ≥ 2 compartidos. Las del propio
    asunto van primero."""
    family = family_of(conn, user_id, asunto_id)
    scope = "AND t.asunto_id = ANY(:aids)" if family else ""
    params: dict[str, Any] = {
        "u": user_id,
        "title": title,
        "statuses": list(statuses),
        "aid": asunto_id if asunto_id is not None else -1,
    }
    if family:
        params["aids"] = family
    rows = (
        conn.execute(
            text(
                f"""
                SELECT {_COLS}, (memex_norm(t.title) = memex_norm(:title)) AS exact
                {_FROM}
                WHERE t.user_id = :u AND t.status = ANY(:statuses)
                  AND COALESCE((t.metadata->>'is_parent')::boolean, false) IS NOT TRUE
                  {scope}
                ORDER BY exact DESC, (t.asunto_id = :aid) DESC, t.id DESC
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    drop = _family_tokens(conn, user_id, family)
    wanted = _tokens(title, drop=drop)
    best: tuple[float, dict[str, Any]] | None = None
    for r in rows:
        row = dict(r)
        row.pop("exact", None)
        if r["exact"]:
            return _public(row)
        other = _tokens(str(r["title"]), drop=drop)
        shared = len(wanted & other)
        cont = token_containment(wanted, other)
        if (
            shared >= PROVENANCE_MIN_SHARED_TOKENS
            and cont >= PROVENANCE_TOKEN_THRESHOLD
            and (best is None or cont > best[0])
        ):
            best = (cont, _public(row))
    return best[1] if best else None


def find_live_semantic(
    conn: Connection, user_id: int, *, title: str, asunto_id: int | None
) -> tuple[dict[str, Any], float] | None:
    """La tarea VIVA (pendiente|vencida) de la familia del asunto que más se parece a `title` por
    SIGNIFICADO, cuando por título (`find_task_by_title`) no hubo ninguna: «you're on the list for
    X» confirma «Inscribirte en X y elegir track» aunque compartan pocas palabras. Devuelve la
    tarea y el coseno; quien la use la pasa por el juez. None sin asunto, sin vivas o sin vecina."""
    family = family_of(conn, user_id, asunto_id)
    if not family:
        return None
    rows = (
        conn.execute(
            text(
                f"""
                SELECT {_COLS} {_FROM}
                WHERE t.user_id = :u AND t.asunto_id = ANY(:aids)
                  AND t.status IN ('pendiente', 'vencida')
                  AND COALESCE((t.metadata->>'is_parent')::boolean, false) IS NOT TRUE
                ORDER BY (t.asunto_id = :aid) DESC, t.id
                """
            ),
            {"u": user_id, "aids": family, "aid": asunto_id},
        )
        .mappings()
        .all()
    )
    sem = semantic_neighbors(conn, user_id, title, rows)
    if not sem:
        return None
    rid, score = max(sem.items(), key=lambda kv: (kv[1], -kv[0]))
    row = next(dict(r) for r in rows if int(r["id"]) == rid)
    return _public(row), score


def complete_from_evidence(
    conn: Connection,
    user_id: int,
    *,
    title: str,
    asunto_id: int | None,
    source_inbox_ids: Sequence[int],
    evidence: str,
    when: datetime | None,
    target_id: int | None = None,
) -> tuple[int, str]:
    """Un mensaje que es la entrega/confirmación de una tarea (`kind='hecha'`: el propio de la
    persona, o el del organizador dirigido a ella): completa la pendiente (o vencida) que
    corresponde —`done_at` = fecha del mensaje— y deja la evidencia; si ya estaba completada, solo
    suma procedencia; si no existe, la registra ya completada. `target_id`: la viva que ya se
    decidió completar (p. ej. la vecina por significado que el juez confirmó); sin él se busca por
    título en la familia del asunto. Devuelve `(tarea_id, 'completed' | 'already' | 'created')`."""
    done_meta = {
        "done_evidence": evidence.strip(),
        "done_inbox_ids": [int(i) for i in source_inbox_ids],
    }
    live: dict[str, Any] | None = None
    done: dict[str, Any] | None = None
    if target_id is not None:
        row = (
            conn.execute(
                text(f"SELECT {_COLS} {_FROM} WHERE t.user_id = :u AND t.id = :t"),
                {"u": user_id, "t": target_id},
            )
            .mappings()
            .first()
        )
        if row is not None and row["status"] in (STATUS_PENDIENTE, STATUS_VENCIDA):
            live = _public(dict(row))
        elif row is not None and row["status"] == STATUS_COMPLETADA:
            done = _public(dict(row))
    if live is None and done is None:
        live = find_task_by_title(
            conn,
            user_id,
            title=title,
            asunto_id=asunto_id,
            statuses=(STATUS_PENDIENTE, STATUS_VENCIDA),
        )
    if live is not None:
        tid = int(live["id"])
        conn.execute(
            text(
                """
                UPDATE mod_tareas
                   SET metadata = metadata || CAST(:m AS JSONB),
                       source_inbox_ids = ARRAY(
                           SELECT DISTINCT x
                             FROM unnest(source_inbox_ids || CAST(:ids AS BIGINT[])) AS x
                       )
                 WHERE user_id = :u AND id = :t
                """
            ),
            {"m": json.dumps(done_meta), "ids": list(source_inbox_ids), "u": user_id, "t": tid},
        )
        set_status(conn, user_id, tid, STATUS_COMPLETADA, when=when)
        _log.info("tareas.completed_from_evidence", user_id=user_id, tarea_id=tid, how="completed")
        return tid, "completed"
    if done is None:
        done = find_task_by_title(
            conn, user_id, title=title, asunto_id=asunto_id, statuses=(STATUS_COMPLETADA,)
        )
    if done is not None:
        tid = int(done["id"])
        conn.execute(
            text(
                """
                UPDATE mod_tareas
                   SET source_inbox_ids = ARRAY(
                           SELECT DISTINCT x
                             FROM unnest(source_inbox_ids || CAST(:ids AS BIGINT[])) AS x
                       ),
                       updated_at = NOW()
                 WHERE user_id = :u AND id = :t
                """
            ),
            {"ids": list(source_inbox_ids), "u": user_id, "t": tid},
        )
        return tid, "already"
    created = add(
        conn,
        user_id,
        title=title,
        asunto=asunto_id,
        metadata=done_meta,
        source_inbox_ids=source_inbox_ids,
        evidence=evidence,
        origin="extraction",
        force=True,
    )
    tid = int(created["id"])
    set_status(conn, user_id, tid, STATUS_COMPLETADA, when=when)
    _log.info("tareas.completed_from_evidence", user_id=user_id, tarea_id=tid, how="created")
    return tid, "created"


def date_next_session_for(
    conn: Connection, user_id: int, tarea: Mapping[str, Any], asunto_id: int
) -> date | None:
    """La fecha que le tocaba a una tarea «para la próxima sesión» si se hubiera sabido el asunto
    al extraerla: la sesión siguiente al día de su mensaje, según el horario del curso. None si
    no era de próxima sesión, ya tiene fecha, o el asunto no tiene horario."""
    from memex.modules.tareas.extract import _message_day, _next_session

    meta = dict(tarea.get("metadata") or {})
    if meta.get("due_kind") != "proxima_sesion" or tarea.get("due_on"):
        return None
    day = _message_day(conn, [int(i) for i in (tarea.get("source_inbox_ids") or [])])
    if day is None:
        return None
    return _next_session(conn, user_id, asunto_id, day)


def adopt_orphan_tareas(conn: Connection, user_id: int, asunto_id: int, inbox_id: int) -> int:
    """Cuando asuntos asienta evidencia sobre un mensaje, las tareas EXTRAÍDAS de ese mensaje que
    nacieron sueltas (la extracción corre antes que la asignación: una nota de Notion no tiene chat
    que diga de qué curso es) se cuelgan del asunto y se teje la arista. Y hacen lo que la
    extracción hubiera hecho de haber sabido el asunto: fechar lo «para la próxima sesión» con el
    horario del curso y quedar marcadas (`metadata.dedup_pending`) para que el re-chequeo de
    duplicados (`extract.recheck_duplicates`, con juez) las compare con las del asunto.
    Idempotente; nunca mueve una tarea que ya tiene asunto. Devuelve cuántas."""
    rows = conn.execute(
        text(
            """
            UPDATE mod_tareas SET asunto_id = :a, updated_at = NOW()
             WHERE user_id = :u AND asunto_id IS NULL AND origin = 'extraction'
               AND :i = ANY(source_inbox_ids)
            RETURNING id
            """
        ),
        {"a": asunto_id, "u": user_id, "i": inbox_id},
    ).all()
    dated = merged = 0
    for (tid,) in rows:
        cur = get(conn, user_id, int(tid))
        if is_parent(cur):
            # Un PROYECTO que nació suelto: si el asunto ya tenía ese proyecto con otro nombre
            # («Proyecto de Instagram» vs «Proyecto de Instagram en Canva»), es el mismo; se funde
            # en vez de quedar repetido con sus pasos aparte.
            twin = find_parent(
                conn, user_id, title=str(cur["title"]), asunto_id=asunto_id, exclude_id=int(tid)
            )
            if twin is not None:
                merge_parents(conn, user_id, survivor_id=twin, absorbed_id=int(tid))
                merged += 1
                continue
        due = date_next_session_for(conn, user_id, cur, asunto_id)
        extra: dict[str, Any] = {"dedup_pending": True}
        if due is not None:
            extra["due_source"] = "proxima_sesion"
            dated += 1
        conn.execute(
            text(
                "UPDATE mod_tareas SET due_on = COALESCE(due_on, :d), "
                "metadata = metadata || CAST(:m AS JSONB), updated_at = NOW() WHERE id = :t"
            ),
            {"d": due, "m": json.dumps(extra), "t": int(tid)},
        )
        weave_tarea(conn, user_id, int(tid))
    if rows:
        _log.info(
            "tareas.adopted_by_asunto",
            user_id=user_id,
            asunto_id=asunto_id,
            inbox_id=inbox_id,
            tareas=len(rows),
            dated=dated,
            parents_merged=merged,
        )
    return len(rows)


def merge_tareas(conn: Connection, user_id: int, *, into: int, absorb: int) -> list[str]:
    """Funde dos tareas YA registradas que son la misma: `absorb` enriquece a `into` (redacción
    como alias, detalle, cita, fecha si faltaba, prioridad si sube, enlaces, procedencia, proyecto
    si `into` no tenía) y queda cancelada con `metadata.merged_into`, así el extractor no la vuelve
    a crear si relee su mensaje. La usa el re-chequeo con juez y el dueño a mano
    (`memex tareas merge`). Devuelve los cambios aplicados a `into`."""
    from memex.modules.tareas.extract import _fixed_day_of, _message_day

    if into == absorb:
        raise ValueError("una tarea no se funde consigo misma")
    dst, src = get(conn, user_id, into), get(conn, user_id, absorb)
    if str(dst.get("status")) == STATUS_CANCELADA:
        raise ValueError(f"la tarea #{into} está cancelada; fundí al revés")
    if (src.get("metadata") or {}).get("is_parent"):
        raise ValueError(f"la tarea #{absorb} es un proyecto: movele los pasos antes de fundirla")
    src_meta = dict(src.get("metadata") or {})
    src_ids = [int(i) for i in (src.get("source_inbox_ids") or [])]
    changes = enrich(
        conn,
        user_id,
        into,
        title=str(src.get("title") or ""),
        description=str(src.get("description") or ""),
        evidence=str(src.get("evidence") or ""),
        due_on=date.fromisoformat(src["due_on"]) if src.get("due_on") else None,
        message_day=_message_day(conn, src_ids),
        fixed_day=_fixed_day_of(conn, dst),
        priority=str(src.get("priority") or "") or None,
        links=[str(u) for u in (src_meta.get("links") or [])],
        source_inbox_ids=src_ids,
        parent=src.get("parent_id") if dst.get("parent_id") is None else None,
    )
    # las otras redacciones de la absorbida también son alias de la que queda
    extra_aliases = [str(a) for a in (src_meta.get("aliases") or [])]
    if extra_aliases:
        fresh = dict(get(conn, user_id, into).get("metadata") or {})
        known = {normalize_match(a) for a in fresh.get("aliases") or []}
        known.add(normalize_match(str(dst.get("title") or "")))
        merged = list(fresh.get("aliases") or [])
        for a in extra_aliases:
            if normalize_match(a) not in known:
                merged.append(a)
                known.add(normalize_match(a))
        conn.execute(
            text(
                "UPDATE mod_tareas SET metadata = metadata || CAST(:m AS JSONB), "
                "updated_at = NOW() WHERE id = :t"
            ),
            {"m": json.dumps({"aliases": merged[-ENRICH_MAX_ALIASES:]}), "t": into},
        )
    set_status(conn, user_id, absorb, STATUS_CANCELADA)
    from memex.semantic.store import delete_vectors

    # La absorbida ya no compite como vecina: su redacción vive en `into` como alias.
    delete_vectors(conn, user_id, SEMANTIC_SLUG, [absorb])
    conn.execute(
        text(
            "UPDATE mod_tareas SET metadata = metadata || CAST(:m AS JSONB), updated_at = NOW() "
            "WHERE id = :t"
        ),
        {"m": json.dumps({"merged_into": into, "dedup_pending": False}), "t": absorb},
    )
    _log.info("tareas.merged", user_id=user_id, into=into, absorb=absorb, changes=changes)
    return changes


def clear_dedup_pending(conn: Connection, user_id: int, tarea_ids: Sequence[int]) -> None:
    if not tarea_ids:
        return
    conn.execute(
        text(
            "UPDATE mod_tareas SET metadata = metadata - 'dedup_pending' "
            "WHERE user_id = :u AND id = ANY(:ids)"
        ),
        {"u": user_id, "ids": [int(i) for i in tarea_ids]},
    )


def relink_orphans(conn: Connection, user_id: int, *, apply: bool) -> dict[str, int]:
    """Tareas EXTRAÍDAS sin asunto: vuelve a resolverlo con la regla de hoy (evidencia del mensaje →
    asunto dominante del chat → nombre que dio el LLM) y, si se sabe el asunto y la tarea era «para
    la próxima sesión», le pone la fecha. Para cuando la regla mejora después de extraer."""
    from memex.modules.tareas.extract import (
        _message_day,
        _next_session,
        resolve_asunto_for,
    )

    stats = {"orphans": 0, "linked": 0, "dated": 0, "remerged": 0}
    # Tareas colgadas de un asunto YA FUNDIDO en otro: pasan al superviviente (siguiendo la
    # cadena). `merge_asuntos` lo hace al fundir desde 2026-09-25; esto recoge lo anterior.
    remerged: set[int] = set()
    for _ in range(8):
        merged = conn.execute(
            text(
                """
                SELECT t.id, a.merged_into_id FROM mod_tareas t
                  JOIN mod_asuntos a ON a.id = t.asunto_id
                 WHERE t.user_id = :u AND a.merged_into_id IS NOT NULL
                 ORDER BY t.id
                """
            ),
            {"u": user_id},
        ).all()
        if not merged:
            break
        remerged.update(int(tid) for tid, _t in merged)
        if not apply:
            break
        for tid, target in merged:
            conn.execute(
                text("UPDATE mod_tareas SET asunto_id = :a, updated_at = NOW() WHERE id = :t"),
                {"a": int(target), "t": int(tid)},
            )
            weave_tarea(conn, user_id, int(tid))
    stats["remerged"] = len(remerged)
    rows = conn.execute(
        text(
            """
            SELECT id, source_inbox_ids, metadata FROM mod_tareas
             WHERE user_id = :u AND origin = 'extraction' AND asunto_id IS NULL
               AND status = 'pendiente'
             ORDER BY id
            """
        ),
        {"u": user_id},
    ).all()
    stats["orphans"] = len(rows)
    for tid, inbox_ids, metadata in rows:
        meta = dict(metadata or {})
        ids = [int(i) for i in (inbox_ids or [])]
        asunto_id = resolve_asunto_for(conn, user_id, ids, str(meta.get("asunto_hint") or ""))
        if asunto_id is None:
            continue
        stats["linked"] += 1
        due_on: date | None = None
        if meta.get("due_kind") == "proxima_sesion":
            day = _message_day(conn, ids)
            if day is not None:
                due_on = _next_session(conn, user_id, asunto_id, day)
        if due_on is not None:
            stats["dated"] += 1
        if apply:
            conn.execute(
                text(
                    """
                    UPDATE mod_tareas
                       SET asunto_id = :a,
                           due_on = COALESCE(due_on, :d),
                           metadata = metadata || CAST(:m AS JSONB),
                           updated_at = NOW()
                     WHERE id = :t
                    """
                ),
                {
                    "a": asunto_id,
                    "d": due_on,
                    "m": json.dumps({"due_source": "proxima_sesion"} if due_on else {}),
                    "t": int(tid),
                },
            )
            weave_tarea(conn, user_id, int(tid))
    # Las que YA tienen asunto pero quedaron sin fecha de próxima sesión (se engancharon antes
    # de que el enganche fechara): misma regla.
    con_asunto = conn.execute(
        text(
            """
            SELECT id, asunto_id FROM mod_tareas
             WHERE user_id = :u AND origin = 'extraction' AND asunto_id IS NOT NULL
               AND status = 'pendiente' AND due_on IS NULL
               AND metadata->>'due_kind' = 'proxima_sesion'
             ORDER BY id
            """
        ),
        {"u": user_id},
    ).all()
    stats["sin_fecha"] = len(con_asunto)
    for tid, aid in con_asunto:
        due = date_next_session_for(conn, user_id, get(conn, user_id, int(tid)), int(aid))
        if due is None:
            continue
        stats["dated"] += 1
        if apply:
            conn.execute(
                text(
                    "UPDATE mod_tareas SET due_on = :d, metadata = metadata || CAST(:m AS JSONB), "
                    "updated_at = NOW() WHERE id = :t"
                ),
                {"d": due, "m": json.dumps({"due_source": "proxima_sesion"}), "t": int(tid)},
            )
    _log.info("tareas.relink_orphans", user_id=user_id, applied=apply, **stats)
    return stats


def discard(
    conn: Connection,
    user_id: int,
    tarea_id: int,
    *,
    reason: str = DISCARD_NOT_A_TASK,
    note: str = "",
) -> dict[str, Any]:
    """Corrección del usuario: «esto no es una tarea». Queda cancelada con `discard_reason`, deja de
    ser vértice, BLOQUEA que la extracción la vuelva a crear (misma procedencia o mismo título) y se
    registra como feedback `wrong_extraction` sobre el mensaje de origen (captura diferida: el
    prompt se revisa a mano con esa evidencia, no se auto-ajusta)."""
    from memex.core.feedback import record_feedback

    current = get(conn, user_id, tarea_id)
    conn.execute(
        text(
            """
            UPDATE mod_tareas
               SET metadata = metadata || CAST(:m AS JSONB), updated_at = NOW()
             WHERE user_id = :u AND id = :t
            """
        ),
        {
            "m": json.dumps(
                {
                    "discard_reason": reason,
                    "discard_note": note,
                    "discarded_at": datetime.now(UTC).isoformat(),
                }
            ),
            "u": user_id,
            "t": tarea_id,
        },
    )
    row = set_status(conn, user_id, tarea_id, STATUS_CANCELADA)
    for iid in current.get("source_inbox_ids") or []:
        record_feedback(
            conn,
            user_id=user_id,
            inbox_id=int(iid),
            kinds=["wrong_extraction"],
            note=f"tarea #{tarea_id} descartada ({reason}): {current['title']}"
            + (f" — {note}" if note else ""),
            metadata={"module": "tareas", "tarea_id": tarea_id, "reason": reason},
        )
    _log.info("tareas.discarded", user_id=user_id, tarea_id=tarea_id, reason=reason)
    return row


def expire_overdue(
    conn: Connection, user_id: int, *, today: date | None = None, apply: bool = True
) -> list[int]:
    """Cierra como `vencida` las pendientes cuya fecha ya pasó y que estaban atadas a una sesión
    (`metadata.due_kind = proxima_sesion`: la fecha era la clase) o pertenecen a un asunto de tipo
    curso, hackatón o evento (`EXPIRING_ASUNTO_KINDS`); las SIN fecha de un hackatón o evento
    cuyo mensaje de origen tiene más de `STALE_UNDATED_DAYS` días (el evento ya pasó); y los
    PROYECTOS de esos asuntos cuya entrega (la mayor fecha de sus pasos, todos con fecha) pasó hace
    más que su gracia (`ritmo.gracia_proyecto`: días según el ritmo del asunto). Un PASO nunca
    vence por su propia fecha: sigue al proyecto (se ve atrasado) y vence solo cuando el proyecto
    vence. Una tarea con fecha explícita de otro tipo de asunto sigue pendiente y vencida a la
    vista (como en Todoist): quizá todavía se pueda hacer. Devuelve los ids afectados, pasos
    incluidos (o los que afectaría, con `apply=False`)."""
    from memex.modules.tareas.ritmo import gracia_proyecto

    ref_day = today or _today()
    rows = conn.execute(
        text(
            """
            SELECT t.id FROM mod_tareas t
              LEFT JOIN mod_asuntos a ON a.id = t.asunto_id
             WHERE t.user_id = :u AND t.status = 'pendiente'
               -- un PASO de un proyecto sigue al proyecto: no vence por la clase
               AND t.parent_id IS NULL
               AND (
                 (t.due_on IS NOT NULL AND t.due_on < :today
                  AND (t.metadata->>'due_kind' = 'proxima_sesion' OR a.kind = ANY(:kinds)))
                 OR
                 -- evento/hackatón: sin fecha y con el mensaje de origen viejo = el evento pasó
                 (t.due_on IS NULL AND a.kind = ANY(:stale_kinds)
                  AND cardinality(t.source_inbox_ids) > 0
                  AND (SELECT MAX(i.occurred_at) FROM inbox i WHERE i.id = ANY(t.source_inbox_ids))
                      < CAST(:today AS DATE) - CAST(:stale_days AS INTEGER))
               )
             ORDER BY t.id
            """
        ),
        {
            "u": user_id,
            "today": ref_day,
            "kinds": list(EXPIRING_ASUNTO_KINDS),
            "stale_kinds": list(STALE_UNDATED_KINDS),
            "stale_days": STALE_UNDATED_DAYS,
        },
    ).all()
    ids = [int(r[0]) for r in rows]
    # Proyectos: candidatos con TODOS sus pasos fechados y la entrega ya pasada; la gracia la
    # pone el ritmo del asunto (una dinámica: días; un proyecto de curso: semanas).
    candidatos = conn.execute(
        text(
            """
            SELECT t.id, t.asunto_id,
                   (SELECT max(c.due_on) FROM mod_tareas c
                     WHERE c.parent_id = t.id AND c.status <> 'cancelada') AS entrega
              FROM mod_tareas t JOIN mod_asuntos a ON a.id = t.asunto_id
             WHERE t.user_id = :u AND t.status = 'pendiente' AND t.parent_id IS NULL
               AND t.due_on IS NULL AND a.kind = ANY(:kinds)
               AND EXISTS (SELECT 1 FROM mod_tareas c
                            WHERE c.parent_id = t.id AND c.status <> 'cancelada')
               AND NOT EXISTS (SELECT 1 FROM mod_tareas c
                                WHERE c.parent_id = t.id AND c.status <> 'cancelada'
                                  AND (c.due_on IS NULL OR c.due_on >= CAST(:today AS DATE)))
             ORDER BY t.id
            """
        ),
        {"u": user_id, "today": ref_day, "kinds": list(EXPIRING_ASUNTO_KINDS)},
    ).all()
    for pid, aid, entrega in candidatos:
        if not isinstance(entrega, date):
            continue
        gracia = gracia_proyecto(conn, user_id, int(pid), int(aid) if aid is not None else None)
        if (ref_day - entrega).days > gracia:
            ids.append(int(pid))
    if ids:
        # Los pasos pendientes de un proyecto vencido vencen con él (siguen al proyecto).
        steps = conn.execute(
            text(
                "SELECT id FROM mod_tareas WHERE user_id = :u AND status = 'pendiente' "
                "AND parent_id = ANY(:ids) ORDER BY id"
            ),
            {"u": user_id, "ids": ids},
        ).all()
        ids.extend(int(r[0]) for r in steps)
    if apply and ids:
        conn.execute(
            text(
                "UPDATE mod_tareas SET status = :s, updated_at = NOW() "
                "WHERE user_id = :u AND id = ANY(:ids)"
            ),
            {"s": STATUS_VENCIDA, "u": user_id, "ids": ids},
        )
        for tid in ids:
            update_vertex(conn, user_id, Ref(TAREA_SLUG, tid))
    _log.info("tareas.expired", user_id=user_id, applied=apply, count=len(ids), today=str(ref_day))
    return ids


def get(conn: Connection, user_id: int, tarea_id: int) -> dict[str, Any]:
    row = (
        conn.execute(
            text(f"SELECT {_COLS} {_FROM} WHERE t.user_id = :u AND t.id = :i"),
            {"u": user_id, "i": tarea_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise TareaNotFoundError(f"no existe la tarea #{tarea_id}")
    return _public(row)


def list_tareas(
    conn: Connection,
    user_id: int,
    *,
    status: str | None = STATUS_PENDIENTE,
    overdue_only: bool = False,
    due_before: date | None = None,
    asunto_id: int | None = None,
    q: str | None = None,
    limit: int = 100,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Tareas del user. `status=None`/'all' = todas. Orden: las de SEGURIDAD primero (un acceso
    que no fue propio no espera), después vencidas, luego fecha límite ascendente, las sin fecha
    al final; dentro, prioridad (alta primero) e id. `category` filtra por una categoría."""
    where = ["t.user_id = :u"]
    params: dict[str, Any] = {"u": user_id, "limit": limit, "today": _today()}
    if category:
        where.append("t.category = :category")
        params["category"] = category_value(category)
    if status and status != "all":
        if status not in STATUSES:
            raise ValueError(f"estado inválido: {status!r}; válidos: {', '.join(STATUSES)}, all")
        where.append("t.status = :status")
        params["status"] = status
    if overdue_only:
        where.append("t.status = 'pendiente' AND t.due_on IS NOT NULL AND t.due_on < :today")
    if due_before is not None:
        where.append("t.due_on IS NOT NULL AND t.due_on <= :due_before")
        params["due_before"] = due_before
    if asunto_id is not None:
        where.append("t.asunto_id = :aid")
        params["aid"] = asunto_id
    if q:
        where.append("(t.title ILIKE :q OR t.description ILIKE :q OR a.name ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    rows = (
        conn.execute(
            text(
                f"""
                SELECT {_COLS} {_FROM}
                WHERE {" AND ".join(where)}
                ORDER BY (t.category = 'seguridad') DESC, (t.due_on IS NULL), t.due_on,
                         t.priority, t.id
                LIMIT :limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    today = params["today"]
    return [_public(r, today=today) for r in rows]


def set_status(
    conn: Connection,
    user_id: int,
    tarea_id: int,
    status: str,
    *,
    when: datetime | None = None,
) -> dict[str, Any]:
    """pendiente | completada | cancelada | vencida. `completada` sella `done_at` (`when` o ahora);
    volver a pendiente lo limpia. Cancelar saca la tarea del grafo (no borra la fila); una vencida
    sigue en el grafo (pasó, pero fue una tarea del asunto)."""
    if status not in STATUSES:
        raise ValueError(f"estado inválido: {status!r}; válidos: {', '.join(STATUSES)}")
    current = get(conn, user_id, tarea_id)
    done_at = (when or datetime.now(UTC)) if status == STATUS_COMPLETADA else None
    conn.execute(
        text(
            "UPDATE mod_tareas SET status = :s, done_at = :d, updated_at = NOW() "
            "WHERE user_id = :u AND id = :i"
        ),
        {"s": status, "d": done_at, "u": user_id, "i": tarea_id},
    )
    ref = Ref(TAREA_SLUG, tarea_id)
    if status == STATUS_CANCELADA:
        delete_vertex(conn, user_id, ref)
    elif current["status"] == STATUS_CANCELADA:
        add_vertex(conn, user_id, ref)
        weave_tarea(conn, user_id, tarea_id)
    else:
        update_vertex(conn, user_id, ref)
    _log.info("tareas.status", user_id=user_id, tarea_id=tarea_id, status=status)
    return get(conn, user_id, tarea_id)


_UNSET: Any = object()


def update(
    conn: Connection,
    user_id: int,
    tarea_id: int,
    *,
    title: str | None = None,
    description: str | None = None,
    due_on: date | None = _UNSET,
    priority: str | int | None = None,
    asunto: str | int | None = _UNSET,
    parent: int | None = _UNSET,
    category: str | None = None,
) -> dict[str, Any]:
    """Corrige campos. `due_on=None` / `asunto=None` / `parent=None` explícitos LIMPIAN; omitidos,
    no tocan. Si cambia el asunto se re-teje la arista (la vieja se retira con el vértice y se
    recrea). `parent` cuelga la tarea como paso de otra (un solo nivel)."""
    current = get(conn, user_id, tarea_id)
    sets: list[str] = []
    params: dict[str, Any] = {"u": user_id, "i": tarea_id}
    if parent is not _UNSET:
        if parent is not None:
            _assert_parent(conn, user_id, parent, child_id=tarea_id)
        sets.append("parent_id = :parent")
        params["parent"] = parent
    if title is not None:
        clean = " ".join(title.split())
        if not clean:
            raise ValueError("el título no puede quedar vacío")
        sets.append("title = :title")
        params["title"] = clean
    if description is not None:
        sets.append("description = :description")
        params["description"] = description.strip()
    if due_on is not _UNSET:
        sets.append("due_on = :due_on")
        params["due_on"] = due_on
    if priority is not None:
        sets.append("priority = :priority")
        params["priority"] = priority_value(priority)
    if category is not None:
        sets.append("category = :category")
        params["category"] = category_value(category)
    asunto_changed = False
    if asunto is not _UNSET:
        new_asunto = resolve_asunto(conn, user_id, asunto)
        if new_asunto != current["asunto_id"]:
            asunto_changed = True
        sets.append("asunto_id = :asunto_id")
        params["asunto_id"] = new_asunto
    if not sets:
        raise ValueError("nada que actualizar: pasá al menos un campo")
    sets.append("updated_at = NOW()")
    conn.execute(
        text(f"UPDATE mod_tareas SET {', '.join(sets)} WHERE user_id = :u AND id = :i"), params
    )
    ref = Ref(TAREA_SLUG, tarea_id)
    if current["status"] != STATUS_CANCELADA:
        if asunto_changed:
            # La arista «parte_de» vieja se va con el vértice; el vértice vuelve y se re-teje.
            delete_vertex(conn, user_id, ref)
            add_vertex(conn, user_id, ref)
            weave_tarea(conn, user_id, tarea_id)
        else:
            update_vertex(conn, user_id, ref)
    if title is not None:
        embed_tarea(conn, user_id, tarea_id, " ".join(title.split()))
    _log.info("tareas.updated", user_id=user_id, tarea_id=tarea_id, fields=sorted(params))
    return get(conn, user_id, tarea_id)


def delete(conn: Connection, user_id: int, tarea_id: int) -> bool:
    """Borra la fila (mantenimiento; el agente usa `cancel`). Saca el vértice del grafo."""
    from memex.semantic.store import delete_vectors

    get(conn, user_id, tarea_id)
    delete_vertex(conn, user_id, Ref(TAREA_SLUG, tarea_id))
    n = conn.execute(
        text("DELETE FROM mod_tareas WHERE user_id = :u AND id = :i"), {"u": user_id, "i": tarea_id}
    ).rowcount
    delete_vectors(conn, user_id, SEMANTIC_SLUG, [tarea_id])
    _log.info("tareas.deleted", user_id=user_id, tarea_id=tarea_id)
    return bool(n)
