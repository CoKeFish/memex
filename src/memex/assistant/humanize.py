"""Un comando `memex …` dicho en palabras, para el chat.

El dueño no quiere ver «memex identidad add --name 'X' --kind persona --email …» en Telegram
(2026-09-17: «me dice cosas de comandos»): lo que se le pide confirmar y lo que se ejecutó se
cuenta en castellano («Guardar contacto: X (persona) · ASOVEC»). El comando exacto sigue en el log
del gateway, que es donde se audita. Determinista: plantillas por verbo; lo que no tiene plantilla
sale como «<dominio> <verbo>: valor, valor».
"""

from __future__ import annotations

import json
import re
import shlex

_MAX = 160


def _parse(command: str) -> tuple[list[str], dict[str, str], list[str]]:
    """(`[dominio, verbo, …posicionales]`, `{flag: valor}`, `[flags sin valor]`)."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if tokens[:2] == ["uv", "run"]:
        tokens = tokens[2:]
    if tokens and tokens[0] == "memex":
        tokens = tokens[1:]
    words: list[str] = []
    flags: dict[str, str] = {}
    bare: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--"):
            key = t[2:]
            if "=" in key:
                k, v = key.split("=", 1)
                flags[k] = v
            elif i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                flags[key] = tokens[i + 1]
                i += 1
            else:
                bare.append(key)
        else:
            words.append(t)
        i += 1
    return words, flags, bare


def _q(s: str) -> str:
    return f"«{s}»"


def _money(flags: dict[str, str]) -> str:
    amount = flags.get("amount", "?")
    cur = flags.get("currency", "")
    try:
        n = float(amount)
        amount = f"{n:,.0f}".replace(",", ".") if n == int(n) else f"{n:,.2f}"
    except ValueError:
        pass
    return f"{amount} {cur}".strip()


def describe(command: str) -> str:
    """«memex identidad add --name 'X' --kind persona --org 'Y'» →
    «Guardar contacto: X (persona) · Y»."""
    words, f, bare = _parse(command)
    if len(words) < 2:
        return command[:_MAX]
    grupo, verbo = words[0], words[1]
    who = f.get("id")
    ref = f"#{who}" if who else ""
    out: str | None = None

    if grupo == "identidad":
        if verbo == "add":
            partes = [p for p in (f.get("org"), f.get("email"), f.get("phone")) if p]
            kind = f" ({f['kind']})" if f.get("kind") else ""
            out = f"Guardar contacto: {f.get('name', '?')}{kind}" + (
                " · " + " · ".join(partes) if partes else ""
            )
        elif verbo == "annotate":
            out = f"Anotar en la ficha {ref}: {_q(f.get('description') or f.get('note') or '…')}"
        elif verbo in ("unify", "set-parent", "affiliate", "relate"):
            out = f"Ficha {ref}: {verbo} " + ", ".join(v for k, v in f.items() if k != "id")
    elif grupo == "finance":
        if verbo == "register":
            sentido = "ingreso" if f.get("direction") == "ingreso" else "gasto"
            cp = f" a {f['counterparty']}" if f.get("counterparty") else ""
            cat = f" ({f['category']})" if f.get("category") else ""
            out = f"Registrar {sentido}: {_money(f)}{cp}{cat}"
        elif verbo == "rubro":
            out = f"Rubro de {_q(f.get('counterparty', '?'))}: {f.get('category', '?')}"
        elif verbo == "rm":
            out = f"Borrar el pago {ref}"
        elif verbo == "set-place":
            out = f"Lugar del pago {ref}: {f.get('text') or f.get('place-id', '?')}"
        elif verbo == "subs-set":
            out = f"Suscripción {ref}: {f.get('status', '?')}"
    elif grupo == "tareas":
        if verbo == "add":
            due = f" (para {f['due']})" if f.get("due") else ""
            asunto = f" · asunto {f['asunto']}" if f.get("asunto") else ""
            out = f"Crear tarea: {_q(f.get('title', '?'))}{due}{asunto}"
        elif verbo == "done":
            out = f"Marcar hecha la tarea {ref}"
        elif verbo == "reopen":
            out = f"Reabrir la tarea {ref}"
        elif verbo == "cancel":
            out = f"Cancelar la tarea {ref}"
        elif verbo == "update":
            cambios = ", ".join(f"{k} {v}" for k, v in f.items() if k != "id")
            out = f"Corregir la tarea {ref}: {cambios or ', '.join(bare)}"
        elif verbo == "merge":
            out = f"Fundir la tarea #{f.get('absorb', '?')} en #{f.get('into', '?')}"
        elif verbo == "link":
            out = f"Sumar enlace a la tarea {ref}: {f.get('url', '?')}"
    elif grupo == "asuntos":
        if verbo == "add":
            kind = f" ({f['kind']})" if f.get("kind") else ""
            out = f"Abrir asunto: {_q(f.get('name', '?'))}{kind}"
        elif verbo == "plan":
            out = f"Armar el plan del asunto {ref} (busca en fuentes oficiales; tarda minutos)"
        elif verbo == "nota":
            out = f"Anotar en el asunto {ref}: {_q(f.get('text', '?'))}"
        elif verbo == "prioridad":
            out = f"Prioridad {f.get('level', '?')} al asunto {ref}"
        elif verbo == "vence":
            out = f"Plazo del asunto {ref}: {f.get('date', '?')}"
        elif verbo == "close":
            out = f"Cerrar el asunto {ref}" + (f": {_q(f['state'])}" if f.get("state") else "")
        elif verbo == "reopen":
            out = f"Reabrir el asunto {ref}"
        elif verbo in ("silence", "unsilence"):
            quien = words[2] if len(words) > 2 else ref
            out = (
                "Silenciar" if verbo == "silence" else "Dejar de silenciar"
            ) + f" el asunto #{quien}"
        elif verbo == "rename":
            out = f"Renombrar el asunto {ref} a {_q(f.get('name', '?'))}"
        elif verbo == "relate":
            tipo = f.get("type", "?")
            out = f"Relacionar asuntos #{f.get('src', '?')} → #{f.get('dst', '?')} ({tipo})"
        elif verbo == "dato":
            out = f"Dato del asunto {ref}: {f.get('label', '?')} = {f.get('value', '?')}"
    elif grupo == "calendario":
        if verbo == "add":
            when = " ".join(
                p for p in (f.get("date") or f.get("on"), f.get("time") or f.get("at")) if p
            )
            out = f"Agendar: {_q(f.get('title', '?'))}" + (f" · {when}" if when else "")
        elif verbo == "rm":
            out = f"Borrar el evento {ref}"
        elif verbo == "update":
            out = f"Corregir el evento {ref}"
        elif verbo in ("fijar", "fijar-regla"):
            out = f"Fijar el evento {ref}"
    elif grupo == "bienestar" and verbo == "register":
        out = f"Registrar bienestar: {f.get('activity') or f.get('category', '?')}"
    elif grupo == "geo" and words[2:3] == ["add"]:
        out = f"Lugar de referencia: {f.get('name', '?')} · {f.get('address', '?')}"

    if out is None:
        valores = [v for k, v in f.items() if k not in ("json", "user")][:3]
        out = f"{grupo} {verbo}" + (": " + ", ".join(valores) if valores else "")
    return out[:_MAX]


_ID_RE = re.compile(r"\bid=\d+\s*|#\d+\s*")
_WS_RE = re.compile(r"[ \t]+")


def plain_detail(output: str, *, max_chars: int = 220) -> str:
    """La salida del CLI sin sus códigos internos, para el chat: «creada: id=1730 'X' (persona) ·
    afiliada a #1729 'Y'» → «creada: 'X' (persona) · afiliada a 'Y'». Un JSON (salida para el
    agente) no se muestra: el chat no es para leer JSON."""
    text = output.strip()
    if not text:
        return ""
    if text[0] in "{[":
        try:
            json.loads(text)
        except ValueError:
            pass
        else:
            return ""
    lines = [_WS_RE.sub(" ", _ID_RE.sub("", ln)).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("{")]
    out = " · ".join(lines[:2])
    return out[: max_chars - 1] + "…" if len(out) > max_chars else out


__all__ = ["describe", "plain_detail"]
