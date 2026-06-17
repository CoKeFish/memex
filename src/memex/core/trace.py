"""Traza jerárquica por mensaje — el "stack trace" de la extracción (ADR-015 / tabla `trace_nodes`).

Un módulo emite nodos SOLO vía `ctx.trace` (un `TraceNode`), nunca importa este módulo ni toca la DB
directo (mismo aislamiento que el resto del contrato). Vive en `core/` porque el orquestador y el
endpoint lo usan y `core` no puede depender de `modules`.

Modelo: una lista PLANA con `parent_id` (self-FK) que el front arma como árbol. Cada `TraceNode`
es un HANDLE reutilizable ligado a una conexión: `.entity/.step/.log/.decision/.llm(...)` INSERTAN
un hijo y devuelven el handle del hijo (→ "padres selectivos": guardás el handle y le colgás hijos
después, aun fuera de un `with`). `__enter__/__exit__` son azúcar (`with paso:`); el auto-enganche
de `record_llm_call` por contextvar llega en el slice del worker async.

El COSTO no se persiste acá: los nodos `llm` referencian `llm_calls(id)` y el roll-up jerárquico se
calcula al leer (`read_trace`), que además cuelga del root las `llm_calls` atribuidas al inbox
(ruteo/extracción/OCR) como hojas `llm`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.db import connection

# --- escritura: root + handle ------------------------------------------------------- #


class Tracer(Protocol):
    """Lo que un módulo ve como `ctx.trace`. Lo satisfacen `TraceNode` (un span fijo —o
    `NULL_TRACER`, no-op—) y `ModuleTracer` (rutea cada entidad al root de SU mensaje cuando la
    unidad abarca un lote). Los módulos SOLO llaman `.entity(...)` directo y cuelgan el resto
    (`step/decision/log`) del handle que devuelve."""

    def entity(
        self,
        table: str,
        *,
        id: int,
        label: str,
        status: str | None = None,
        detail: dict[str, Any] | None = None,
        source_inbox_ids: Sequence[int] | None = None,
    ) -> TraceNode: ...


def create_root(conn: Connection, *, user_id: int, inbox_id: int, label: str) -> int:
    """Borra la traza previa del inbox (delete-then-write: re-extraer reemplaza) y crea el nodo
    `root`. Devuelve su id. Escribe en `conn` (el caller decide la tx)."""
    conn.execute(
        text("DELETE FROM trace_nodes WHERE user_id = :u AND inbox_id = :i"),
        {"u": user_id, "i": inbox_id},
    )
    rid = conn.execute(
        text(
            """
            INSERT INTO trace_nodes (user_id, inbox_id, parent_id, seq, kind, label)
            VALUES (:u, :i, NULL, 0, 'root', :l)
            RETURNING id
            """
        ),
        {"u": user_id, "i": inbox_id, "l": label},
    ).scalar_one()
    return int(rid)


class TraceNode:
    """Handle de un nodo del árbol. Sus métodos insertan un HIJO y devuelven su handle. Un handle
    "inactivo" (`conn`/`id` None = `NULL_TRACER`) hace no-op en todo, así un módulo llama
    `ctx.trace.*` sin chequear si la traza está encendida."""

    def __init__(
        self, conn: Connection | None, *, user_id: int, inbox_id: int | None, node_id: int | None
    ) -> None:
        self._conn = conn
        self._user_id = user_id
        self._inbox_id = inbox_id
        self.id = node_id
        self._next_seq = 0

    @property
    def _active(self) -> bool:
        return self._conn is not None and self.id is not None

    def _child(
        self,
        kind: str,
        label: str,
        *,
        status: str | None = None,
        detail: dict[str, Any] | None = None,
        ref: tuple[str, int] | None = None,
        llm_call_id: int | None = None,
    ) -> TraceNode:
        if not self._active:
            return self  # NULL_TRACER / inactivo
        assert self._conn is not None
        seq = self._next_seq
        self._next_seq += 1
        new_id = self._conn.execute(
            text(
                """
                INSERT INTO trace_nodes
                  (user_id, inbox_id, parent_id, seq, kind, label, status, ref_table, ref_id,
                   llm_call_id, detail)
                VALUES
                  (:u, :i, :p, :s, :k, :l, :st, :rt, :ri, :lc, CAST(:d AS JSONB))
                RETURNING id
                """
            ),
            {
                "u": self._user_id,
                "i": self._inbox_id,
                "p": self.id,
                "s": seq,
                "k": kind,
                "l": label,
                "st": status,
                "rt": ref[0] if ref else None,
                "ri": ref[1] if ref else None,
                "lc": llm_call_id,
                "d": json.dumps(detail or {}, default=str, ensure_ascii=False),
            },
        ).scalar_one()
        return TraceNode(
            self._conn, user_id=self._user_id, inbox_id=self._inbox_id, node_id=int(new_id)
        )

    def entity(
        self,
        table: str,
        *,
        id: int,
        label: str,
        status: str | None = None,
        detail: dict[str, Any] | None = None,
        source_inbox_ids: Sequence[int] | None = None,
    ) -> TraceNode:
        """Nodo de ENTIDAD: referencia (no copia) una fila de dominio (`table`+`id`). El front la
        muestra como `#id` linkeable y le cuelga abajo los pasos de qué le pasó.

        Convención: `entity` = fila de dominio TOCADA por el mensaje (creada O referenciada), no
        estrictamente "insertada en esta corrida". finance/calendar/hackathones emiten una por fila
        materializada; identidades emite una por MENCIÓN resuelta, anclada a la identidad resuelta
        —que puede ser preexistente—. Por eso el invariante de cobertura exige `≥1` entity cuando un
        módulo persistió filas, no una correspondencia 1:1.

        `source_inbox_ids` lo acepta por el contrato `Tracer` (lo pasa el módulo), pero este nodo lo
        IGNORA —ya está fijado a un mensaje—; quien lo usa para rutear al root correcto es
        `ModuleTracer`."""
        del source_inbox_ids
        return self._child("entity", label, status=status, detail=detail, ref=(table, id))

    def step(
        self, label: str, *, status: str | None = None, detail: dict[str, Any] | None = None
    ) -> TraceNode:
        """Paso/agrupador interno (p. ej. 'dedup')."""
        return self._child("step", label, status=status, detail=detail)

    def log(
        self, label: str, *, status: str | None = None, detail: dict[str, Any] | None = None
    ) -> TraceNode:
        """Hoja informativa (p. ej. 'creada nueva')."""
        return self._child("log", label, status=status, detail=detail)

    def decision(
        self,
        label: str,
        *,
        ref: tuple[str, int] | None = None,
        status: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> TraceNode:
        """Punto de decisión (p. ej. 'vs tx #87' en dedup, o 'contraparte → identidad'). `ref` ata
        la CONTRAPARTE comparada (linkeable)."""
        return self._child("decision", label, status=status, detail=detail, ref=ref)

    def llm(
        self,
        call_id: int,
        *,
        label: str = "LLM",
        status: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> TraceNode:
        """Nodo de una llamada LLM ya registrada en `llm_calls` (su costo/output crudo se leen de
        ahí). Para colgar el desempate de un dedup bajo su comparación (worker async, slice
        posterior)."""
        return self._child("llm", label, status=status, detail=detail, llm_call_id=call_id)

    def __enter__(self) -> TraceNode:
        return self

    def __exit__(self, *exc: object) -> None:
        return  # no suprime excepciones


#: Tracer no-op: lo recibe un módulo cuando la traza está apagada (batch / window multi-mensaje).
NULL_TRACER = TraceNode(None, user_id=0, inbox_id=None, node_id=None)


def open_module_tracer(
    conn: Connection,
    *,
    user_id: int,
    inbox_id: int,
    root_id: int | None,
    slug: str,
    label: str,
    seq: int,
) -> TraceNode:
    """Abre el span de un módulo bajo el root y devuelve el handle posicionado ahí (el módulo le
    cuelga sus entidades/pasos). `root_id` None → `NULL_TRACER` (sin traza). Escribe en `conn` (la
    tx del módulo, atómico con su persist)."""
    if root_id is None:
        return NULL_TRACER
    new_id = conn.execute(
        text(
            """
            INSERT INTO trace_nodes (user_id, inbox_id, parent_id, seq, kind, module_slug, label)
            VALUES (:u, :i, :p, :s, 'module', :ms, :l)
            RETURNING id
            """
        ),
        {"u": user_id, "i": inbox_id, "p": root_id, "s": seq, "ms": slug, "l": label},
    ).scalar_one()
    return TraceNode(conn, user_id=user_id, inbox_id=inbox_id, node_id=int(new_id))


class ModuleTracer:
    """`ctx.trace` de un módulo sobre una UNIDAD que puede abarcar varios mensajes de un lote. Rutea
    cada `.entity(...)` al span del módulo bajo el root del mensaje que la originó
    (`source_inbox_ids[0]`), abriendo ese span perezosamente y cacheándolo por inbox_id —así la
    entidad de cada mensaje cuelga de SU árbol, no del primero del lote. Satisface `Tracer`. El span
    de un módulo es lazy: si no emite entidades para un mensaje, ese root no gana un nodo vacío."""

    def __init__(
        self,
        conn: Connection,
        *,
        user_id: int,
        trace_roots: Mapping[int, int],
        slug: str,
        seq: int,
    ) -> None:
        self._conn = conn
        self._user_id = user_id
        self._roots = trace_roots
        self._slug = slug
        self._seq = seq
        self._spans: dict[int, TraceNode] = {}

    def _span(self, inbox_id: int) -> TraceNode:
        span = self._spans.get(inbox_id)
        if span is None:
            span = open_module_tracer(
                self._conn,
                user_id=self._user_id,
                inbox_id=inbox_id,
                root_id=self._roots[inbox_id],
                slug=self._slug,
                label=self._slug,
                seq=self._seq,
            )
            self._spans[inbox_id] = span
        return span

    def entity(
        self,
        table: str,
        *,
        id: int,
        label: str,
        status: str | None = None,
        detail: dict[str, Any] | None = None,
        source_inbox_ids: Sequence[int] | None = None,
    ) -> TraceNode:
        """Cuelga la entidad del span del módulo bajo el root de `source_inbox_ids[0]`. Sin
        atribución a un mensaje de la ventana (vacío o fuera de ella) → no-op."""
        iid = source_inbox_ids[0] if source_inbox_ids else None
        if iid is None or iid not in self._roots:
            return NULL_TRACER
        return self._span(iid).entity(table, id=id, label=label, status=status, detail=detail)


def attach_to_entity(
    conn: Connection, *, user_id: int, table: str, ref_id: int
) -> TraceNode | None:
    """Handle posicionado en el nodo `entity` que referencia `(table, ref_id)`, o None si no existe
    (mensaje batch / nunca extraído por-mensaje). Lo usa un worker ASÍNCRONO (p. ej. el desempate
    LLM de dedup FASE-2) para colgarle nodos a la entidad creada en la extracción — padres
    selectivos a posteriori. Escribe en `conn` (la tx del worker)."""
    row = (
        conn.execute(
            text(
                """
                SELECT id, inbox_id FROM trace_nodes
                WHERE user_id = :u AND kind = 'entity' AND ref_table = :t AND ref_id = :r
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {"u": user_id, "t": table, "r": ref_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return TraceNode(conn, user_id=user_id, inbox_id=row["inbox_id"], node_id=int(row["id"]))


def attach_to_root(conn: Connection, *, user_id: int, inbox_id: int) -> TraceNode | None:
    """Handle posicionado en el nodo `root` del mensaje `inbox_id`, o None si no existe (el mensaje
    nunca se extrajo por-mensaje). Para un worker ASÍNCRONO per-mensaje que NO produce una fila de
    dominio con su propio nodo `entity` (p. ej. la co-ocurrencia de identidades, que materializa
    `relation_edges`): cuelga su llamada LLM directo bajo el root del mensaje. Escribe en `conn`."""
    row = (
        conn.execute(
            text(
                """
                SELECT id FROM trace_nodes
                WHERE user_id = :u AND inbox_id = :i AND kind = 'root'
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {"u": user_id, "i": inbox_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return TraceNode(conn, user_id=user_id, inbox_id=inbox_id, node_id=int(row["id"]))


# --- lectura: serialización del árbol + roll-up de costo ----------------------------- #


def _purpose_label(purpose: str) -> str:
    if purpose == "module_route":
        return "Ruteo"
    if purpose == "extract_grouped":
        return "Extracción agrupada"
    if purpose.startswith("extract_"):
        return f"Extracción · {purpose[len('extract_') :]}"
    if purpose == "ocr":
        return "OCR · visión"
    return purpose


def _node_status_from_llm(s: str) -> str:
    if s == "ok":
        return "ok"
    if s == "error":
        return "error"
    return "info"  # filtered / omitido


def _llm_payload(c: Any) -> dict[str, Any]:
    return {
        "model": c["model"],
        "promptTokens": int(c["prompt_tokens"]),
        "completionTokens": int(c["completion_tokens"]),
        "latencyMs": int(c["latency_ms"]),
        "status": c["status"],
        "responseText": c["response_text"],
    }


def _to_float(v: Any) -> float:
    return float(v) if isinstance(v, (Decimal, int, float)) else 0.0


def read_trace(user_id: int, inbox_id: int) -> list[dict[str, Any]] | None:
    """Árbol de traza del inbox (`TraceNodeDto[]`, lista plana camelCase) para `GET /inbox/{id}`.

    Devuelve None si no hay nodos persistidos (→ empty-state en el front). Cuelga del `root` las
    `llm_calls WHERE inbox_id` (ruteo/extracción/OCR) como hojas `llm` sintéticas —salvo las ya
    referenciadas por un nodo `llm` explícito (dedupe por `llm_call_id`)— y calcula el roll-up de
    costo (`ownUsd`/`subtreeUsd`/`calls`) con DFS post-orden.
    """
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT id, parent_id, seq, kind, module_slug, label, status, ref_table, ref_id,
                           llm_call_id, detail, created_at
                    FROM trace_nodes
                    WHERE user_id = :u AND inbox_id = :i
                    ORDER BY parent_id NULLS FIRST, seq, id
                    """
                ),
                {"u": user_id, "i": inbox_id},
            )
            .mappings()
            .all()
        )
        if not rows:
            return None
        # Solo las llm_calls de la corrida ACTUAL: desde la creación del root (delete-then-write al
        # re-extraer) en adelante — así re-extraer NO apila ruteo/extracción de corridas viejas.
        root_row = next((r for r in rows if r["parent_id"] is None and r["kind"] == "root"), None)
        root_created = root_row["created_at"] if root_row else None
        call_sql = """
            SELECT id, purpose, model, prompt_tokens, completion_tokens, cost_usd,
                   latency_ms, status, response_text, created_at
            FROM llm_calls
            WHERE user_id = :u AND inbox_id = :i
        """
        params: dict[str, Any] = {"u": user_id, "i": inbox_id}
        if root_created is not None:
            call_sql += " AND created_at >= :rc"
            params["rc"] = root_created
        call_sql += " ORDER BY created_at, id"
        calls = conn.execute(text(call_sql), params).mappings().all()
        # Calls de nodos `llm` EXPLÍCITOS (p. ej. el desempate FASE-2 de un worker async, con
        # inbox_id=NULL) NO salen filtradas por inbox → se traen por id para tener costo/output.
        explicit_ids = sorted(
            {
                int(r["llm_call_id"])
                for r in rows
                if r["kind"] == "llm" and r["llm_call_id"] is not None
            }
        )
        have = {int(c["id"]) for c in calls}
        missing = [i for i in explicit_ids if i not in have]
        extra_calls: list[Any] = []
        if missing:
            extra_calls = list(
                conn.execute(
                    text(
                        """
                        SELECT id, purpose, model, prompt_tokens, completion_tokens, cost_usd,
                               latency_ms, status, response_text, created_at
                        FROM llm_calls
                        WHERE user_id = :u AND id = ANY(CAST(:ids AS BIGINT[]))
                        """
                    ),
                    {"u": user_id, "ids": missing},
                ).mappings()
            )
        # Reparto del costo de una call COMPARTIDA: cuántos nodos `llm` (de CUALQUIER inbox) la
        # referencian. 1 nodo (mensaje individual / desempate async colgado a uno) → costo completo;
        # N nodos (una `extract_grouped`/ruteo de lote colgada a cada mensaje del lote) → cost/N por
        # nodo. El COUNT de nodos es la verdad (no `metadata["n"]`: miente con co/leftover).
        share_by_call: dict[int, int] = {}
        if explicit_ids:
            share_by_call = {
                int(cr["llm_call_id"]): int(cr["n"])
                for cr in conn.execute(
                    text(
                        """
                        SELECT llm_call_id, COUNT(*) AS n FROM trace_nodes
                        WHERE user_id = :u AND llm_call_id = ANY(CAST(:ids AS BIGINT[]))
                        GROUP BY llm_call_id
                        """
                    ),
                    {"u": user_id, "ids": explicit_ids},
                ).mappings()
            }

    root_id = root_row["id"] if root_row is not None else None
    call_by_id: dict[Any, Any] = {c["id"]: c for c in calls}
    for c in extra_calls:
        call_by_id[c["id"]] = c
    explicit_call_ids = set(explicit_ids)

    out: list[dict[str, Any]] = []
    own: dict[int, float] = {}  # id → costo propio

    for r in rows:
        payload = None
        own_cost = 0.0
        if r["kind"] == "llm" and r["llm_call_id"] is not None:
            c = call_by_id.get(r["llm_call_id"])
            if c is not None:
                # cost/N si la call es compartida (N nodos la referencian); N=1 → costo completo.
                own_cost = _to_float(c["cost_usd"]) / share_by_call.get(int(r["llm_call_id"]), 1)
                payload = _llm_payload(c)
        node = {
            "id": int(r["id"]),
            "parentId": int(r["parent_id"]) if r["parent_id"] is not None else None,
            "seq": int(r["seq"]),
            "kind": r["kind"],
            "moduleSlug": r["module_slug"],
            "label": r["label"],
            "status": r["status"],
            "ref": {"table": r["ref_table"], "id": int(r["ref_id"])} if r["ref_table"] else None,
            "llmCallId": int(r["llm_call_id"]) if r["llm_call_id"] is not None else None,
            "detail": r["detail"] or {},
            "llm": payload,
        }
        own[node["id"]] = own_cost
        out.append(node)

    # Hojas `llm` sintéticas: las llm_calls del inbox no referenciadas explícitamente, bajo el root.
    for idx, c in enumerate(calls):
        if c["id"] in explicit_call_ids:
            continue
        nid = -int(c["id"])  # id negativo: no colisiona con los bigserial
        node = {
            "id": nid,
            "parentId": root_id,
            "seq": -1000 + idx,  # antes que los spans de módulo
            "kind": "llm",
            "moduleSlug": None,
            "label": _purpose_label(c["purpose"]),
            "status": _node_status_from_llm(c["status"]),
            "ref": None,
            "llmCallId": int(c["id"]),
            "detail": {},
            "llm": _llm_payload(c),
        }
        own[nid] = _to_float(c["cost_usd"])
        out.append(node)

    # Roll-up de costo: DFS post-orden sobre el árbol.
    by_id = {n["id"]: n for n in out}
    children: dict[int | None, list[int]] = {}
    for n in out:
        children.setdefault(n["parentId"], []).append(n["id"])

    def rollup(nid: int) -> tuple[float, int]:
        n = by_id[nid]
        sub = own.get(nid, 0.0)
        n_calls = 1 if (n["kind"] == "llm" and n["llmCallId"] is not None) else 0
        for cid in children.get(nid, []):
            cs, cc = rollup(cid)
            sub += cs
            n_calls += cc
        n["cost"] = {
            "ownUsd": round(own.get(nid, 0.0), 6),
            "subtreeUsd": round(sub, 6),
            "calls": n_calls,
        }
        return sub, n_calls

    for n in out:
        if n["parentId"] is None or n["parentId"] not in by_id:
            rollup(n["id"])

    return out
