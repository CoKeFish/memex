"""Origen de una transacción: de qué MENSAJES salió y qué OBSERVADOR la reportó.

El dedup FASE 1 (`dedup.py`, puro) recibe esto ya resuelto en cada `DedupRow`:

- `source_inbox_ids` → veto de ORIGEN: dos filas extraídas del mismo mensaje o documento son
  movimientos distintos por construcción (un extracto no lista dos veces el mismo movimiento; dos
  giros iguales en días seguidos son dos giros).
- `observers` → FUENTES INDEPENDIENTES: un observador es QUIÉN reporta el movimiento —el remitente
  de un correo (`email:<dominio>`), un chat (`chat:<id>`), un documento aportado (`documentos`), el
  agente (`agente`). Observadores disjuntos (el extracto del banco y el correo del comercio; el
  aviso de la pasarela y el recibo de la tienda) ven el mismo cobro desde dos sistemas: suma
  evidencia y el nombre de la contraparte deja de poder vetar, porque el intermediario y el comercio
  se nombran distinto. Dos alertas del MISMO banco o dos recibos del MISMO comercio no son
  independientes: ahí dos cafés iguales el mismo día siguen siendo dos cafés.

Acá vive lo que toca la DB o el registro de fuentes para resolverlo, y las etiquetas en castellano
que ve el juez de la FASE 2 (`dedup_llm.py`). Sin marcas: solo el TIPO de fuente (y el remitente
tal como viene en el dato).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.modules.finance.dedup import KIND_AGENT
from memex.sources import kind_for_type

#: Separador entre el tipo de fuente y la pista del observador en lo que devuelve el SQL.
HINT_SEP = "\t"

#: Etiqueta por categoría de fuente, para el juez LLM.
SOURCE_KIND_LABELS: dict[str, str] = {
    "email": "correo",
    "chat": "chat",
    "social": "red social",
    "notes": "notas",
    "documentos": "documento aportado (p. ej. extracto bancario)",
    KIND_AGENT: "registro del agente",
}

#: Pista del observador por mensaje, en SQL: el email del remitente (correo) o el chat (chat).
OBSERVER_HINT_SQL = "COALESCE(i.payload->'from'->>'email', i.payload->>'chat_id', '')"


def observer_for(source_type: str, hint: str | None) -> str | None:
    """`<categoría>` o `<categoría>:<quién>` para un mensaje según el tipo de su fuente y la pista
    (`OBSERVER_HINT_SQL`). Correo → dominio del remitente; chat → id del chat; el resto solo la
    categoría. `None` si el tipo no tiene categoría registrada."""
    try:
        kind = kind_for_type(source_type).value
    except KeyError:
        return None
    who = (hint or "").strip().lower()
    if kind == "email":
        who = who.rsplit("@", 1)[-1] if "@" in who else ""
    elif kind != "chat":
        who = ""
    return f"{kind}:{who}" if who else kind


def kind_of(observer: str) -> str:
    """Categoría de fuente de un observador (`email:banco.com` → `email`)."""
    return observer.split(":", 1)[0]


def observers_for_origin(
    type_hints: Iterable[str] | None, source_inbox_ids: Iterable[int]
) -> frozenset[str]:
    """Observadores de una fila a partir de `"<tipo>\\t<pista>"` por mensaje (lo que agrega el
    SQL). Sin mensaje de origen (`source_inbox_ids` vacío) es un registro del agente →
    `{KIND_AGENT}`. Un tipo sin categoría se ignora (no inventa independencia)."""
    if not tuple(source_inbox_ids):
        return frozenset({KIND_AGENT})
    out: set[str] = set()
    for item in type_hints or ():
        source_type, _, hint = str(item).partition(HINT_SEP)
        obs = observer_for(source_type, hint)
        if obs is not None:
            out.add(obs)
    return frozenset(out)


def observers_by_inbox(conn: Connection, user_id: int, inbox_ids: Sequence[int]) -> dict[int, str]:
    """`inbox.id → observador` de los mensajes del lote (JOIN a `sources.type` + pista)."""
    ids = list(inbox_ids)
    if not ids:
        return {}
    rows = conn.execute(
        text(
            f"SELECT i.id, s.type, {OBSERVER_HINT_SQL} AS hint "
            "FROM inbox i JOIN sources s ON s.id = i.source_id "
            "WHERE i.user_id = :uid AND i.id = ANY(:ids)"
        ),
        {"uid": user_id, "ids": ids},
    ).all()
    out: dict[int, str] = {}
    for inbox_id, source_type, hint in rows:
        obs = observer_for(str(source_type), None if hint is None else str(hint))
        if obs is not None:
            out[int(inbox_id)] = obs
    return out


def label_for_observers(observers: Iterable[str]) -> str:
    """Etiqueta legible para el juez: 'correo de banco.com', 'documento aportado (…)',
    'chat', 'registro del agente'; varios se unen con ' + '."""
    labels: set[str] = set()
    for obs in observers:
        kind, _, who = obs.partition(":")
        base = SOURCE_KIND_LABELS.get(kind, kind)
        labels.add(f"{base} de {who}" if kind == "email" and who else base)
    return " + ".join(sorted(labels))
