"""Eventos multi-hecho del agente: `start` → `register*` (staged) → `end` (procesa atómico).

El agente (Hermes) abre un evento con `start`, registra N hechos (identidad / finanzas / bienestar)
que se CACHEAN en `mod_agent_event_facts` SIN persistir, y al cerrar con `end` se procesan JUNTOS,
en UNA sola transacción, en orden de dependencia (identidad → finanzas → bienestar): la identidad se
crea primero y finanzas ata su contraparte por ID (determinista, saltea el match-exacto por nombre).
`dedup` + `consolidación` + aristas corren DENTRO de los `register()` de cada dominio — acá NO se
reimplementa lógica de dominio, solo se ordena y se enlaza.

Un evento ABIERTO por usuario (índice parcial único en DB). `end` guarda su resultado en el evento:
reintentarlo es idempotente (devuelve lo guardado). Si `end` falla a mitad, la tx hace rollback y el
evento queda `open` → reintentable (los hechos staged siguen ahí).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.modules.bienestar import cli as bienestar_cli
from memex.modules.finance import cli as finance_cli
from memex.modules.identidades import cli as identidades_cli
from memex.modules.identidades.normalize import normalize_match

_log = get_logger("memex.agent_event")

#: Orden de dependencia al cerrar: identidad ANTES que finanzas (para atar la contraparte por id);
#: bienestar al final (no tiene dependencias).
_KIND_ORDER: dict[str, int] = {"identidad": 0, "finance": 1, "bienestar": 2}

#: kind → parser del dominio (para re-parsear el argv staged en el cierre).
_PARSERS: dict[str, Callable[[], argparse.ArgumentParser]] = {
    "identidad": identidades_cli._build_parser,
    "finance": finance_cli._build_parser,
    "bienestar": bienestar_cli._build_parser,
}


class AgentEventError(Exception):
    """Error de FLUJO del evento (no hay evento abierto, ya hay uno abierto, …). Mensaje para el
    agente; no es un fallo interno."""


def has_open_event(conn: Connection, user_id: int) -> bool:
    """¿El user tiene un evento abierto? (el umbrella decide: encolar vs persistir)."""
    return (
        conn.execute(
            text("SELECT 1 FROM mod_agent_event WHERE user_id = :u AND status = 'open'"),
            {"u": user_id},
        ).scalar()
        is not None
    )


def start_event(conn: Connection, user_id: int) -> dict[str, Any]:
    """Abre el evento del user. Falla si ya hay uno abierto. `event_id = 'agent-<id>'`."""
    if has_open_event(conn, user_id):
        raise AgentEventError(
            "ya hay un evento abierto; cerralo con 'memex end' o descartalo con 'memex cancel'."
        )
    new_id = int(
        conn.execute(
            text("INSERT INTO mod_agent_event (user_id, status) VALUES (:u, 'open') RETURNING id"),
            {"u": user_id},
        ).scalar_one()
    )
    event_id = f"agent-{new_id}"
    conn.execute(
        text("UPDATE mod_agent_event SET event_id = :e WHERE id = :id"),
        {"e": event_id, "id": new_id},
    )
    _log.info("agent_event.started", user_id=user_id, event_id=event_id)
    return {"event_id": event_id, "status": "open"}


def stage_fact(conn: Connection, user_id: int, kind: str, argv: list[str]) -> dict[str, Any]:
    """Encola un hecho (su `argv` crudo) en el evento abierto. Valida que el `argv` parsee con el
    parser del dominio (feedback inmediato al agente); el procesamiento real es en `end`."""
    ev = (
        conn.execute(
            text("SELECT id, event_id FROM mod_agent_event WHERE user_id = :u AND status = 'open'"),
            {"u": user_id},
        )
        .mappings()
        .first()
    )
    if ev is None:
        raise AgentEventError("no hay un evento abierto; abrí uno con 'memex start'.")
    parse_fact(kind, argv)  # valida; lanza ValueError si el argv está mal
    conn.execute(
        text(
            "INSERT INTO mod_agent_event_facts (event_fk, user_id, kind, argv) "
            "VALUES (:e, :u, :k, CAST(:argv AS JSONB))"
        ),
        {"e": int(ev["id"]), "u": user_id, "k": kind, "argv": json.dumps(argv)},
    )
    count = int(
        conn.execute(
            text("SELECT count(*) FROM mod_agent_event_facts WHERE event_fk = :e"),
            {"e": int(ev["id"])},
        ).scalar_one()
    )
    return {"staged": True, "kind": kind, "event_id": str(ev["event_id"]), "count": count}


def cancel_event(conn: Connection, user_id: int) -> dict[str, Any]:
    """Descarta el evento abierto + sus hechos staged (CASCADE). Falla si no hay ninguno abierto."""
    row = conn.execute(
        text(
            "DELETE FROM mod_agent_event WHERE user_id = :u AND status = 'open' RETURNING event_id"
        ),
        {"u": user_id},
    ).first()
    if row is None:
        raise AgentEventError("no hay un evento abierto que cancelar.")
    _log.info("agent_event.cancelled", user_id=user_id, event_id=row[0])
    return {"cancelled": True, "event_id": row[0]}


def close_event(conn: Connection, user_id: int) -> dict[str, Any]:
    """Cierra el evento abierto: procesa los hechos staged en orden de dependencia, en ESTA tx.
    Idempotente: sin abierto pero con uno cerrado, devuelve su resultado (`already_closed`)."""
    ev = (
        conn.execute(
            text(
                "SELECT id, event_id FROM mod_agent_event "
                "WHERE user_id = :u AND status = 'open' FOR UPDATE"
            ),
            {"u": user_id},
        )
        .mappings()
        .first()
    )
    if ev is None:
        prev = conn.execute(
            text(
                "SELECT result FROM mod_agent_event WHERE user_id = :u AND status = 'closed' "
                "AND result IS NOT NULL ORDER BY id DESC LIMIT 1"
            ),
            {"u": user_id},
        ).scalar()
        if prev is not None:
            saved = prev if isinstance(prev, dict) else json.loads(prev)
            return {**saved, "already_closed": True}
        raise AgentEventError("no hay un evento abierto que cerrar.")

    event_id = str(ev["event_id"])
    facts = (
        conn.execute(
            text("SELECT kind, argv FROM mod_agent_event_facts WHERE event_fk = :e ORDER BY id"),
            {"e": int(ev["id"])},
        )
        .mappings()
        .all()
    )
    ordered = sorted(facts, key=lambda f: _KIND_ORDER[str(f["kind"])])
    name_to_id: dict[str, int] = {}
    out: dict[str, list[dict[str, Any]]] = {"identidad": [], "finance": [], "bienestar": []}
    for f in ordered:
        kind = str(f["kind"])
        args = parse_fact(kind, list(f["argv"]))
        if kind == "identidad":
            row = identidades_cli.register_add_from_args(conn, user_id, args, event_id=event_id)
            _index_identity(name_to_id, args, row)
            out["identidad"].append(row)
        elif kind == "finance":
            cid = name_to_id.get(normalize_match(args.counterparty)) if args.counterparty else None
            row = finance_cli.register_from_args(
                conn, user_id, args, event_id=event_id, counterparty_identity_id=cid
            )
            out["finance"].append(row)
        else:  # bienestar
            row = bienestar_cli.register_from_args(conn, user_id, args, event_id=event_id)
            out["bienestar"].append(row)

    result: dict[str, Any] = {
        "event_id": event_id,
        "identidad": out["identidad"],
        "finance": out["finance"],
        "bienestar": out["bienestar"],
        "counts": {k: len(v) for k, v in out.items()},
    }
    conn.execute(
        text(
            "UPDATE mod_agent_event SET status = 'closed', closed_at = NOW(), "
            "result = CAST(:r AS JSONB) WHERE id = :id"
        ),
        {"r": json.dumps(result, default=str), "id": int(ev["id"])},
    )
    _log.info("agent_event.closed", user_id=user_id, event_id=event_id, **result["counts"])
    return result


def parse_fact(kind: str, argv: list[str]) -> argparse.Namespace:
    """Parsea el `argv` staged con el parser del dominio. `ValueError` si está mal (argparse haría
    `sys.exit`; lo convertimos para que el cierre haga rollback limpio sin matar el proceso)."""
    factory = _PARSERS.get(kind)
    if factory is None:
        raise ValueError(f"kind desconocido: {kind!r}")
    try:
        return factory().parse_args(argv)
    except SystemExit as exc:
        raise ValueError(f"argumentos inválidos para {kind}: {' '.join(argv)}") from exc


def _index_identity(
    name_to_id: dict[str, int], args: argparse.Namespace, row: dict[str, Any]
) -> None:
    """Indexa la identidad creada/resuelta por su nombre (el que dio el agente y el canónico) y, si
    trajo empresa, también la org → finanzas ata su contraparte por id en el MISMO evento."""
    iid = int(row["id"])
    for name in (args.name, row.get("display_name")):
        key = normalize_match(str(name)) if name else ""
        if key:
            name_to_id[key] = iid
    org = row.get("org")
    if org:
        okey = normalize_match(str(org["display_name"]))
        if okey:
            name_to_id[okey] = int(org["id"])
