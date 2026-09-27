"""Zona horaria LOCAL del usuario para interpretar horas «peladas» (sin zona) sacadas del texto.

Las horas escritas en un recibo, una invitación o un comprobante («17:24», «3 pm») son hora local
de quien recibe el correo, no UTC. Cuando un módulo tiene que convertirlas a un instante absoluto
(TIMESTAMPTZ) debe combinarlas con ESTA zona: combinarlas con UTC corría todo 5 h (bug de
finanzas, 2026-09-04). Hoy es una constante (single-user; ver memoria `user-timezone-bogota`);
cuando haya configuración por usuario, este es el único punto a cambiar.

Zona EXPLÍCITA: si el mensaje dice la zona («10:00 UTC», «3 pm GMT-5»), el extractor la captura
en un campo aparte (`occurred_tz` en finanzas, `tz` en calendario) como desplazamiento `±HH:MM`,
`UTC` o nombre IANA; `parse_tz` la resuelve y gana sobre la local. Si no la dice, se asume local.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOCAL_TZ_NAME = "America/Bogota"
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)

_UTC_WORDS = frozenset({"utc", "gmt", "z", "zulu", "+00:00", "-00:00", "+0000", "gmt+0", "utc+0"})
_OFFSET_RE = re.compile(r"^(?:utc|gmt)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def normalize_tz(value: object) -> str | None:
    """Normaliza lo que escribe el LLM a una forma canónica o None si no es una zona reconocible.

    `UTC`/`GMT`/`Z` → `"UTC"`; `-5`, `GMT-5`, `UTC-05:00`, `-0500` → `"-05:00"`; nombre IANA
    válido (`America/Bogota`) → tal cual. Abreviaturas ambiguas (`PT`, `EST`) NO se aceptan: el
    prompt pide el desplazamiento; si el modelo las manda igual, se descartan (se asume local) en
    vez de adivinar."""
    s = str(value or "").strip()
    if not s:
        return None
    low = s.lower()
    if low in _UTC_WORDS:
        return "UTC"
    m = _OFFSET_RE.match(s)
    if m:
        sign, hh, mm = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        if hh > 14 or mm > 59:
            return None
        if hh == 0 and mm == 0:
            return "UTC"
        return f"{sign}{hh:02d}:{mm:02d}"
    if "/" in s:
        try:
            ZoneInfo(s)
        except (ZoneInfoNotFoundError, ValueError):
            return None
        return s
    return None


def parse_tz(value: object) -> tzinfo | None:
    """`tzinfo` de una zona normalizable (ver `normalize_tz`); None si no hay o no se reconoce."""
    canon = normalize_tz(value)
    if canon is None:
        return None
    if canon == "UTC":
        return UTC
    if canon[0] in "+-":
        sign = 1 if canon[0] == "+" else -1
        hh, mm = int(canon[1:3]), int(canon[4:6])
        return timezone(sign * timedelta(hours=hh, minutes=mm))
    return ZoneInfo(canon)


def local_instant(d: date, t: time | None = None, tz: tzinfo | None = None) -> datetime:
    """Instante tz-aware a partir de una fecha y (opcional) una hora «peladas».

    `tz` es la zona EXPLÍCITA del mensaje si la dijo; si no, la LOCAL del usuario. Sin hora →
    medianoche de esa zona, no UTC: medianoche UTC se ve como las 19:00 del día ANTERIOR en
    Bogotá, así que una transacción «solo fecha» aparecía un día antes."""
    return datetime.combine(d, t if t is not None else time.min, tzinfo=tz or LOCAL_TZ)


def to_local_wallclock(d: date, t: time, tz: tzinfo) -> tuple[date, time]:
    """Convierte una hora escrita en la zona `tz` a la fecha+hora LOCAL del usuario (para lo que se
    guarda naive en hora local, como calendario): 15:00 UTC → 10:00 Bogotá; 02:00 UTC → 21:00 del
    día anterior."""
    local = datetime.combine(d, t, tzinfo=tz).astimezone(LOCAL_TZ)
    return local.date(), local.time().replace(tzinfo=None)
