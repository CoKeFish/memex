"""Overrides de prompts en la base y su aplicación en cada proceso.

`prompt_overrides`: una fila por clave con el texto vigente (el que se aplica). Cada guardado
deja una fila en `prompt_override_versions` (historial: volver a una versión anterior es un
clic). Restablecer al código borra la fila vigente (el historial queda).

Aplicación: `apply_overrides(conn)` escribe cada override sobre sus targets y restaura el
default del código donde ya no hay override. `refresh_if_stale()` lo hace como mucho cada
`REFRESH_TTL_S` por proceso (una consulta chica: el máximo `updated_at`) y se llama desde
`build_llm_client`, que corre antes de cualquier uso del LLM: la API, el scheduler y el
asistente ven un cambio de la UI en menos de un minuto, sin reiniciar."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.prompts.registry import (
    PROMPTS,
    capture_defaults,
    default_text,
    read_target,
    spec,
    write_target,
)

_log = get_logger("memex.prompts.overrides")

REFRESH_TTL_S = 30.0
MAX_CHARS = 60_000
#: Fragmento que identifica a los principios generales dentro de un prompt (para avisar si una
#: edición los sacó; no bloquea).
_PRINCIPLES_MARK = "PROPIO O PARA TODOS"

_lock = threading.Lock()
_last_refresh: float = 0.0
_last_stamp: datetime | None = None


@dataclass(frozen=True)
class OverrideRow:
    key: str
    text: str
    note: str
    updated_at: datetime


@dataclass(frozen=True)
class VersionRow:
    id: int
    key: str
    text: str
    note: str
    created_at: datetime


@dataclass
class PromptView:
    key: str
    title: str
    group: str
    description: str
    consumer: str
    editable: bool
    default: str
    override: str | None
    note: str
    updated_at: datetime | None
    #: El texto vigente en el proceso de la API (lo que un consumidor leería ahora).
    effective: str
    warnings: list[str] = field(default_factory=list)
    versions: list[VersionRow] = field(default_factory=list)


# --- lectura -------------------------------------------------------------------------------- #


def list_overrides(conn: Connection) -> dict[str, OverrideRow]:
    rows = conn.execute(
        text("SELECT key, text, note, updated_at FROM prompt_overrides ORDER BY key")
    ).all()
    return {str(k): OverrideRow(str(k), str(t), str(n or ""), u) for k, t, n, u in rows}


def versions_of(conn: Connection, key: str, *, limit: int = 20) -> list[VersionRow]:
    rows = conn.execute(
        text(
            "SELECT id, key, text, note, created_at FROM prompt_override_versions "
            "WHERE key = :k ORDER BY id DESC LIMIT :n"
        ),
        {"k": key, "n": limit},
    ).all()
    return [VersionRow(int(i), str(k), str(t), str(n or ""), c) for i, k, t, n, c in rows]


def validate(key: str, text_: str) -> tuple[str, list[str]]:
    """Normaliza y valida; devuelve (texto, avisos). Lanza ValueError si no se puede guardar."""
    s = spec(key)
    if not s.editable:
        raise ValueError(f"{key} es de referencia: no se edita desde acá")
    clean = text_.replace("\r\n", "\n").strip("\n")
    if not clean.strip():
        raise ValueError("el prompt no puede quedar vacío")
    if len(clean) > MAX_CHARS:
        raise ValueError(f"el prompt supera {MAX_CHARS} caracteres")
    warnings: list[str] = []
    base = default_text(key)
    if _PRINCIPLES_MARK in base and _PRINCIPLES_MARK not in clean:
        warnings.append(
            "El texto ya no incluye los principios generales que el código sí trae "
            "(«PROPIO O PARA TODOS…»)."
        )
    if base.count('"') >= 4 and clean.count("{") < base.count("{") // 2:
        warnings.append("El formato JSON de respuesta parece haber quedado incompleto.")
    return clean, warnings


def view(conn: Connection, key: str, *, with_versions: bool = True) -> PromptView:
    s = spec(key)
    ov = list_overrides(conn).get(key)
    warnings: list[str] = []
    if ov is not None:
        try:
            _, warnings = validate(key, ov.text)
        except ValueError:
            warnings = ["El override guardado no pasa la validación actual."]
    return PromptView(
        key=s.key,
        title=s.title,
        group=s.group,
        description=s.description,
        consumer=s.consumer,
        editable=s.editable,
        default=default_text(key),
        override=ov.text if ov else None,
        note=ov.note if ov else "",
        updated_at=ov.updated_at if ov else None,
        effective=read_target(s.targets[0]),
        warnings=warnings,
        versions=versions_of(conn, key) if with_versions else [],
    )


def list_views(conn: Connection) -> list[PromptView]:
    ovs = list_overrides(conn)
    out: list[PromptView] = []
    for s in PROMPTS:
        ov = ovs.get(s.key)
        out.append(
            PromptView(
                key=s.key,
                title=s.title,
                group=s.group,
                description=s.description,
                consumer=s.consumer,
                editable=s.editable,
                default=default_text(s.key),
                override=ov.text if ov else None,
                note=ov.note if ov else "",
                updated_at=ov.updated_at if ov else None,
                effective=read_target(s.targets[0]),
            )
        )
    return out


# --- escritura ------------------------------------------------------------------------------ #


def set_override(conn: Connection, key: str, text_: str, *, note: str = "") -> PromptView:
    """Guarda el texto como vigente (una versión nueva en el historial) y lo aplica en este
    proceso. Guardar el texto igual al del código equivale a restablecer."""
    clean, _ = validate(key, text_)
    if clean == default_text(key):
        return reset_override(conn, key, note=note or "igual al código")
    conn.execute(
        text(
            "INSERT INTO prompt_overrides (key, text, note, updated_at) "
            "VALUES (:k, :t, :n, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET text = EXCLUDED.text, note = EXCLUDED.note, "
            "updated_at = NOW()"
        ),
        {"k": key, "t": clean, "n": note.strip()[:300]},
    )
    conn.execute(
        text("INSERT INTO prompt_override_versions (key, text, note) VALUES (:k, :t, :n)"),
        {"k": key, "t": clean, "n": note.strip()[:300]},
    )
    _apply_one(key, clean)
    _mark_dirty()
    _log.info("prompts.override_set", key=key, chars=len(clean), note=note[:80])
    return view(conn, key)


def reset_override(conn: Connection, key: str, *, note: str = "") -> PromptView:
    """Vuelve al texto del código: borra la fila vigente (el historial queda) y restaura los
    targets en este proceso."""
    spec(key)
    conn.execute(text("DELETE FROM prompt_overrides WHERE key = :k"), {"k": key})
    conn.execute(
        text("INSERT INTO prompt_override_versions (key, text, note) VALUES (:k, :t, :n)"),
        {"k": key, "t": default_text(key), "n": (note or "restablecido al código")[:300]},
    )
    _apply_one(key, default_text(key))
    _mark_dirty()
    _log.info("prompts.override_reset", key=key)
    return view(conn, key)


def revert_to_version(conn: Connection, key: str, version_id: int) -> PromptView:
    row = conn.execute(
        text("SELECT text, note FROM prompt_override_versions WHERE id = :i AND key = :k"),
        {"i": version_id, "k": key},
    ).first()
    if row is None:
        raise KeyError(f"versión {version_id} no existe para {key}")
    return set_override(conn, key, str(row[0]), note=f"vuelta a la versión {version_id}")


# --- aplicación ----------------------------------------------------------------------------- #


def _apply_one(key: str, value: str) -> None:
    for target in spec(key).targets:
        try:
            write_target(target, value)
        except Exception as exc:  # pragma: no cover - defensivo (target renombrado)
            _log.warning("prompts.apply_failed", key=key, target=target, error=str(exc)[:200])


def apply_overrides(conn: Connection) -> int:
    """Escribe los overrides vigentes sobre sus targets y restaura el código donde no hay.
    Devuelve cuántas claves tienen override."""
    capture_defaults()
    ovs = list_overrides(conn)
    for s in PROMPTS:
        ov = ovs.get(s.key)
        _apply_one(s.key, ov.text if ov else default_text(s.key))
    return len(ovs)


def _stamp(conn: Connection) -> datetime | None:
    val: Any = conn.execute(text("SELECT max(updated_at) FROM prompt_overrides")).scalar()
    return val if isinstance(val, datetime) else None


def _mark_dirty() -> None:
    global _last_refresh
    with _lock:
        _last_refresh = 0.0


def refresh_if_stale(*, force: bool = False) -> bool:
    """Reaplica los overrides si pasó el TTL y cambió el sello (o `force`). Best-effort: sin
    tabla (migración pendiente) o sin DB, no falla. Devuelve True si reaplicó."""
    global _last_refresh, _last_stamp
    now = time.monotonic()
    with _lock:
        if not force and now - _last_refresh < REFRESH_TTL_S:
            return False
        _last_refresh = now
    try:
        from memex.db import connection

        with connection() as conn:
            stamp = _stamp(conn)
            if not force and stamp == _last_stamp and _last_stamp is not None:
                return False
            n = apply_overrides(conn)
            _last_stamp = stamp
            if n:
                _log.info("prompts.overrides_applied", overrides=n)
            return True
    except Exception as exc:  # pragma: no cover - defensivo
        _log.warning("prompts.refresh_failed", error=str(exc)[:200])
        return False
