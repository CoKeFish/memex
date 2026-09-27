"""Datos clave de un asunto: lo que hace falta tener a mano para participar —el código de clase de
la plataforma, el enlace de acceso, el salón, el número de expediente o de reserva, el usuario—.
No son tareas ni hitos: son hechos pequeños que un mensaje suelta una vez y luego nadie encuentra.

Viven en `mod_asuntos.metadata.datos` como lista de `{label, value, inbox_ids, evidence, origin,
at}`; se deduplican por valor (normalizado). Los pone el extractor de tareas (`kind='dato'`), el
dashboard o la CLI (`memex-asuntos dato`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger

_log = get_logger("memex.modules.asuntos.datos")

MAX_DATOS = 50
#: Etiqueta cuando el mensaje/usuario no dio una: en una fusión cede ante una etiqueta real.
PLACEHOLDER_LABEL = "dato"

#: Un dato clave NUNCA es un secreto: contraseñas, PIN, OTP, claves temporales o de acceso no se
#: guardan en `metadata` (texto plano). Para credenciales está el vault cifrado; un correo que
#: trae la contraseña inicial de una cuenta se queda como evidencia, pero el dato no se copia.
_SECRET_RE = re.compile(
    r"contrase[ñn]a|password|passcode|passwd|\bpin\b|\botp\b|\btoken\b|secret|secreto|\bcvv\b"
    r"|clave (?:de acceso|temporal|inicial|provisional|secreta)|c[oó]digo de verificaci[oó]n",
    re.IGNORECASE,
)


class SecretDatoError(ValueError):
    """El dato parece una credencial: no se guarda."""


def is_secret(label: str, value: str = "", evidence: str = "") -> bool:
    return bool(_SECRET_RE.search(f"{label} {evidence}"))


def _norm(value: str) -> str:
    return " ".join(value.split()).casefold()


def datos_of(conn: Connection, user_id: int, asunto_id: int) -> list[dict[str, Any]]:
    raw = conn.execute(
        text("SELECT metadata->'datos' FROM mod_asuntos WHERE user_id = :u AND id = :a"),
        {"u": user_id, "a": asunto_id},
    ).scalar()
    return [d for d in (raw or []) if isinstance(d, dict)]


def _save(conn: Connection, user_id: int, asunto_id: int, datos: list[dict[str, Any]]) -> None:
    conn.execute(
        text(
            "UPDATE mod_asuntos SET metadata = metadata || jsonb_build_object('datos', "
            "CAST(:d AS JSONB)), updated_at = NOW() WHERE user_id = :u AND id = :a"
        ),
        {"d": json.dumps(datos[:MAX_DATOS], ensure_ascii=False), "u": user_id, "a": asunto_id},
    )


def add_dato(
    conn: Connection,
    user_id: int,
    asunto_id: int,
    *,
    label: str,
    value: str,
    source_inbox_ids: Sequence[int] = (),
    evidence: str = "",
    origin: str = "extraction",
) -> bool:
    """Suma un dato al asunto. Mismo valor (normalizado) = el mismo dato: no se repite, solo se le
    suman los mensajes de origen (y la etiqueta si venía vacía). Devuelve True si era nuevo."""
    clean_label = " ".join(label.split())
    clean_value = " ".join(value.split())
    if not clean_value:
        raise ValueError("el dato no puede estar vacío")
    if is_secret(clean_label, clean_value, evidence):
        raise SecretDatoError(
            f"{clean_label or 'dato'}: parece una contraseña o clave; no se guarda como dato"
        )
    ids = [int(i) for i in source_inbox_ids]
    datos = datos_of(conn, user_id, asunto_id)
    for d in datos:
        if _norm(str(d.get("value", ""))) == _norm(clean_value):
            merged = sorted(set(int(i) for i in d.get("inbox_ids", [])) | set(ids))
            d["inbox_ids"] = merged
            if not d.get("label") and clean_label:
                d["label"] = clean_label
            _save(conn, user_id, asunto_id, datos)
            return False
    datos.append(
        {
            "label": clean_label or PLACEHOLDER_LABEL,
            "value": clean_value,
            "inbox_ids": sorted(set(ids)),
            "evidence": evidence.strip()[:300],
            "origin": origin,
            "at": datetime.now(UTC).isoformat(),
        }
    )
    _save(conn, user_id, asunto_id, datos)
    _log.info(
        "asuntos.dato.added",
        user_id=user_id,
        asunto_id=asunto_id,
        label=clean_label[:60],
        origin=origin,
    )
    return True


def remove_dato(conn: Connection, user_id: int, asunto_id: int, value: str) -> bool:
    datos = datos_of(conn, user_id, asunto_id)
    kept = [d for d in datos if _norm(str(d.get("value", ""))) != _norm(value)]
    if len(kept) == len(datos):
        return False
    _save(conn, user_id, asunto_id, kept)
    _log.info("asuntos.dato.removed", user_id=user_id, asunto_id=asunto_id)
    return True


def merge_datos(conn: Connection, user_id: int, *, survivor_id: int, absorbed_id: int) -> int:
    """Pasa los datos del asunto absorbido al superviviente (fusión). Mismo valor normalizado =
    el mismo dato: se unen los mensajes de origen y se conserva la etiqueta del superviviente (o
    la del absorbido si aquella venía vacía). Devuelve cuántos datos eran nuevos para el
    superviviente. Los datos del absorbido no se borran: su fila queda como historia."""
    incoming = datos_of(conn, user_id, absorbed_id)
    if not incoming:
        return 0
    datos = datos_of(conn, user_id, survivor_id)
    by_value = {_norm(str(d.get("value", ""))): d for d in datos}
    added = 0
    for d in incoming:
        key = _norm(str(d.get("value", "")))
        if not key:
            continue
        cur = by_value.get(key)
        if cur is None:
            datos.append(dict(d))
            by_value[key] = datos[-1]
            added += 1
            continue
        cur["inbox_ids"] = sorted(
            set(int(i) for i in cur.get("inbox_ids", []))
            | set(int(i) for i in d.get("inbox_ids", []))
        )
        if cur.get("label") in ("", None, PLACEHOLDER_LABEL) and d.get("label"):
            cur["label"] = d["label"]
    _save(conn, user_id, survivor_id, datos)
    return added
