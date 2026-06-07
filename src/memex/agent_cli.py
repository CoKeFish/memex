"""CLI `memex` — la interfaz ÚNICA del AGENTE (Hermes) sobre memex.

Despacha a las CLIs de dominio (`memex-bienestar`, `memex-finance`, `memex-identidades`) exponiendo
SOLO los comandos que el agente usa para registrar/consultar la vida del usuario; los de
MANTENIMIENTO quedan fuera (blocklist/allowlist). No reimplementa nada: forwardea el resto de argv
al `main` del dominio, así los flags y el `--json` (ÚLTIMA línea de stdout) son idénticos.

Además implementa el flujo de EVENTO multi-hecho (`memex.agent_event`): `start` abre un evento y, a
partir de ahí, los `register` se ENCOLAN (staging) en vez de persistir; `end` los procesa JUNTOS en
una sola transacción (resuelve dependencias → dedup → consolidación), `cancel` los descarta. Un
evento abierto por usuario. Determinista, sin LLM: el agente estructura; memex guarda, deduplica,
consolida y conecta.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from typing import Any

from memex.agent_event import (
    AgentEventError,
    cancel_event,
    close_event,
    has_open_event,
    stage_fact,
    start_event,
)
from memex.db import connection
from memex.modules.bienestar.cli import main as _bienestar_main
from memex.modules.finance.cli import main as _finance_main
from memex.modules.identidades.cli import main as _identidades_main

#: Grupos del agente → `main` de la CLI de dominio a la que se forwardea.
_GROUPS: dict[str, Callable[[list[str]], int]] = {
    "bienestar": _bienestar_main,
    "finance": _finance_main,
    "identidad": _identidades_main,
}

#: Subcomandos de MANTENIMIENTO (no del agente): se bloquean en la superficie `memex`.
_BLOCKED: dict[str, frozenset[str]] = {"finance": frozenset({"dedup", "consolidate"})}

#: Grupos donde el agente SOLO usa estos subcomandos (allowlist): el resto es mantenimiento.
#: `identidad` tiene mucho mantenimiento (sync/merge/…) y un único verbo del agente.
_ALLOWED: dict[str, frozenset[str]] = {"identidad": frozenset({"add", "help"})}

#: (grupo, subcomando) → kind del hecho: los register que se ENCOLAN cuando hay un evento abierto.
_STAGEABLE: dict[tuple[str, str], str] = {
    ("identidad", "add"): "identidad",
    ("finance", "register"): "finance",
    ("bienestar", "register"): "bienestar",
}

#: Comandos top-level del flujo de evento (no son grupos).
_EVENT_CMDS: frozenset[str] = frozenset({"start", "end", "cancel"})

_HELP = """memex — interfaz del agente para registrar y consultar la vida del usuario.

Uso: memex <grupo> <comando> [opciones]   |   memex <start|end|cancel>

bienestar (salud y bienestar):
  memex bienestar register     comida/higiene/ejercicio/grooming/salud
  memex bienestar list         lista registros
  memex bienestar summary      total + conteos
  memex bienestar adherence    adherencia + rachas de hábitos
  memex bienestar habit        define hábitos (add/list/rm)

finance (gastos/ingresos):
  memex finance register       registra una transacción

identidad (directorio de personas/organizaciones):
  memex identidad add          registra/resuelve una tarjeta de contacto (no duplica)

evento multi-hecho (una factura = varios hechos del MISMO evento):
  memex start                  abre un evento (los register siguientes se ENCOLAN, no persisten)
  memex finance register ...   ┐ se acumulan en el evento
  memex identidad add ...      ┘
  memex end                    procesa TODO junto: ata identidad↔gasto, dedup, consolida (atómico)
  memex cancel                 descarta el evento abierto

Reglas:
  start/end     un evento abierto por vez; al cerrar memex resuelve dependencias + dedup + consolida
  --event <id>  (sin start/end) hechos del MISMO mensaje comparten el id
  --json        la respuesta JSON es la ÚLTIMA línea de stdout

Detalle de un comando: memex <grupo> <comando> -h   (o: memex <grupo> help)"""


def _safe(s: str) -> str:
    """Sanea para el encoding de la consola (cp1252 en Windows), como las CLIs de dominio."""
    enc = sys.stdout.encoding or "utf-8"
    return s.encode(enc, errors="replace").decode(enc, errors="replace")


def _say(msg: str, *, err: bool = False) -> None:
    print(_safe(msg), file=sys.stderr if err else sys.stdout)


def _emit_json(obj: object) -> None:
    print(_safe(json.dumps(obj, default=str, ensure_ascii=False)))


def _err(msg: str) -> int:
    print(_safe(msg), file=sys.stderr)
    return 2


def _extract_user(rest: Sequence[str]) -> int:
    """Saca el `--user N` (o `--user=N`) del argv; default 1, como las CLIs de dominio."""
    items = list(rest)
    for i, a in enumerate(items):
        if a == "--user" and i + 1 < len(items):
            try:
                return int(items[i + 1])
            except ValueError:
                return 1
        if a.startswith("--user="):
            try:
                return int(a.split("=", 1)[1])
            except ValueError:
                return 1
    return 1


def _wants_json(rest: Sequence[str]) -> bool:
    return "--json" in rest


def _human_event(cmd: str, result: dict[str, Any]) -> str:
    if cmd == "start":
        return f"evento abierto: {result['event_id']} (los register se encolan hasta 'memex end')."
    if cmd == "cancel":
        return f"evento descartado: {result['event_id']}."
    counts = result.get("counts", {})
    tag = " (ya estaba cerrado)" if result.get("already_closed") else ""
    return (
        f"evento {result['event_id']} cerrado{tag}: identidad={counts.get('identidad', 0)} "
        f"finance={counts.get('finance', 0)} bienestar={counts.get('bienestar', 0)}."
    )


def _event_command(cmd: str, rest: list[str]) -> int:
    """`start` / `end` / `cancel`: cada uno en su propia tx. `end` envuelve TODO el procesamiento:
    un hecho inválido → rollback total y el evento queda abierto (reintentable)."""
    user_id = _extract_user(rest)
    try:
        with connection() as conn:
            if cmd == "start":
                result = start_event(conn, user_id)
            elif cmd == "cancel":
                result = cancel_event(conn, user_id)
            else:  # end
                result = close_event(conn, user_id)
    except AgentEventError as e:
        _say(str(e), err=True)
        return 1
    except Exception as e:  # un hecho inválido en `end`: rollback + mensaje al agente, no traceback
        _say(f"error en 'memex {cmd}': {e}", err=True)
        return 1
    if _wants_json(rest):
        _emit_json(result)
    else:
        _say(_human_event(cmd, result))
    return 0


def _maybe_stage(group: str, rest: list[str]) -> int | None:
    """Si el comando es un `register` stageable y el user tiene un evento ABIERTO, lo ENCOLA y
    devuelve el exit code. Si no hay evento abierto (o no es stageable), devuelve None → el caller
    persiste inmediato (comportamiento de siempre)."""
    kind = _STAGEABLE.get((group, rest[0])) if rest else None
    if kind is None:
        return None
    user_id = _extract_user(rest)
    with connection() as conn:
        if not has_open_event(conn, user_id):
            return None
    try:
        with connection() as conn:
            staged = stage_fact(conn, user_id, kind, rest)
    except (AgentEventError, ValueError) as e:
        _say(str(e), err=True)
        return 1
    if _wants_json(rest):
        _emit_json(staged)
    else:
        _say(
            f"encolado en {staged['event_id']}: {kind} (#{staged['count']}); "
            f"cerrá el evento con 'memex end'."
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("help", "-h", "--help"):
        print(_safe(_HELP))
        return 0
    cmd, rest = args[0], args[1:]
    if cmd in _EVENT_CMDS:
        return _event_command(cmd, rest)
    dispatch = _GROUPS.get(cmd)
    if dispatch is None:
        return _err(f"comando desconocido: '{cmd}'. Probá: memex help")
    if rest and rest[0] in _BLOCKED.get(cmd, frozenset()):
        return _err(f"'{rest[0]}' es de mantenimiento, no del agente; usá 'memex-{cmd}' directo.")
    allowed = _ALLOWED.get(cmd)
    if allowed is not None and rest and rest[0] not in allowed:
        return _err(
            f"'{rest[0]}' no es del agente; con '{cmd}' solo: {', '.join(sorted(allowed))}. "
            f"Mantenimiento: 'memex-{cmd}' directo."
        )
    # Con un evento ABIERTO, los register se ENCOLAN (no persisten) hasta 'memex end'.
    staged = _maybe_stage(cmd, rest)
    if staged is not None:
        return staged
    return dispatch(rest)


if __name__ == "__main__":
    sys.exit(main())
