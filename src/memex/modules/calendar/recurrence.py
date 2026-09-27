"""Recurrencia al estilo Google Calendar (RFC 5545), PURO: sin DB ni LLM.

Modelo: un evento recurrente es UNA fila (el **maestro**) con `recurrence` = lista de líneas
`RRULE:` / `EXDATE:` (las mismas que Google expone en `event.recurrence`). Las ocurrencias NO se
guardan: se **expanden** al leer, dentro de la ventana que se consulta (`occurrences`), así una
regla sin fin («todos los jueves») no cuesta nada. Una ocurrencia modificada o cancelada a mano es
una **excepción**: fila propia que apunta al maestro (`recurring_event_id`) con su
`original_start_on` (el `originalStartTime` de Google); la expansión la usa para reemplazar o
quitar esa ocurrencia.

`RecurrenceSpec` es la forma ESTRUCTURADA que devuelve el extractor («clases de lunes a viernes
hasta el 25») y que acepta el CLI (`--every weekly --byday MO,TU`); `to_rrule` la serializa.
La expansión usa `dateutil.rrule` (ya es dependencia).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

from dateutil.rrule import rrulestr

from memex.core.localtime import LOCAL_TZ

Freq = Literal["daily", "weekly", "monthly", "yearly"]

FREQS: tuple[Freq, ...] = ("daily", "weekly", "monthly", "yearly")
WEEKDAYS: tuple[str, ...] = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

#: Nombres en español/inglés → código RFC 5545 (lo que el extractor o el usuario puedan escribir).
_DAY_ALIASES: dict[str, str] = {
    "mo": "MO",
    "lu": "MO",
    "lun": "MO",
    "lunes": "MO",
    "monday": "MO",
    "mon": "MO",
    "tu": "TU",
    "ma": "TU",
    "mar": "TU",
    "martes": "TU",
    "tuesday": "TU",
    "tue": "TU",
    "we": "WE",
    "mi": "WE",
    "mie": "WE",
    "mié": "WE",
    "miercoles": "WE",
    "miércoles": "WE",
    "wednesday": "WE",
    "wed": "WE",
    "th": "TH",
    "ju": "TH",
    "jue": "TH",
    "jueves": "TH",
    "thursday": "TH",
    "thu": "TH",
    "fr": "FR",
    "vi": "FR",
    "vie": "FR",
    "viernes": "FR",
    "friday": "FR",
    "fri": "FR",
    "sa": "SA",
    "sab": "SA",
    "sáb": "SA",
    "sabado": "SA",
    "sábado": "SA",
    "saturday": "SA",
    "sat": "SA",
    "su": "SU",
    "do": "SU",
    "dom": "SU",
    "domingo": "SU",
    "sunday": "SU",
    "sun": "SU",
}
_FREQ_ALIASES: dict[str, Freq] = {
    "daily": "daily",
    "diario": "daily",
    "diaria": "daily",
    "cada dia": "daily",
    "weekly": "weekly",
    "semanal": "weekly",
    "cada semana": "weekly",
    "monthly": "monthly",
    "mensual": "monthly",
    "cada mes": "monthly",
    "yearly": "yearly",
    "anual": "yearly",
    "cada año": "yearly",
}
_DAY_NAMES_ES = {
    "MO": "lun",
    "TU": "mar",
    "WE": "mié",
    "TH": "jue",
    "FR": "vie",
    "SA": "sáb",
    "SU": "dom",
}
_FREQ_NAMES_ES: dict[str, str] = {
    "DAILY": "diaria",
    "WEEKLY": "semanal",
    "MONTHLY": "mensual",
    "YEARLY": "anual",
}

#: Tope duro de ocurrencias por expansión (una ventana razonable nunca se acerca; protege de una
#: regla rota tipo FREQ=DAILY sobre una ventana de años).
MAX_OCCURRENCES = 2000

#: Prefijo del id de serie de los maestros creados por memex (manual o extracción); los de Google
#: usan el id del evento maestro del proveedor.
SERIES_PREFIX = "memex:"

_RULE_LINE_RE = re.compile(r"^(RRULE|EXDATE|RDATE|EXRULE)[:;]", re.IGNORECASE)


class RecurrenceError(ValueError):
    """Regla de recurrencia inválida o no soportada."""


def normalize_day(value: str) -> str:
    key = value.strip().lower()
    if key in _DAY_ALIASES:
        return _DAY_ALIASES[key]
    if key.upper() in WEEKDAYS:
        return key.upper()
    raise RecurrenceError(f"día inválido: {value!r} (usá MO..SU o lunes..domingo)")


def normalize_freq(value: str) -> Freq:
    key = value.strip().lower()
    if key in _FREQ_ALIASES:
        return _FREQ_ALIASES[key]
    raise RecurrenceError(f"frecuencia inválida: {value!r} (daily|weekly|monthly|yearly)")


@dataclass(frozen=True)
class RecurrenceSpec:
    """Regla estructurada: frecuencia + días (solo semanal) + fin opcional (`until` o `count`).
    Sin `until` ni `count` = serie SIN FIN (como Google)."""

    freq: Freq
    interval: int = 1
    byday: tuple[str, ...] = ()
    until: date | None = None
    count: int | None = None

    def __post_init__(self) -> None:
        if self.freq not in FREQS:
            raise RecurrenceError(f"frecuencia inválida: {self.freq!r}")
        if self.interval < 1:
            raise RecurrenceError("interval debe ser ≥ 1")
        if self.count is not None and self.count < 1:
            raise RecurrenceError("count debe ser ≥ 1")
        if self.until is not None and self.count is not None:
            raise RecurrenceError("until y count son excluyentes")
        bad = [d for d in self.byday if d not in WEEKDAYS]
        if bad:
            raise RecurrenceError(f"byday inválido: {bad}")


def to_rrule(spec: RecurrenceSpec) -> str:
    """Serializa a una línea `RRULE:` RFC 5545 (la forma que Google guarda en `recurrence`)."""
    parts = [f"FREQ={spec.freq.upper()}"]
    if spec.interval != 1:
        parts.append(f"INTERVAL={spec.interval}")
    if spec.byday:
        parts.append("BYDAY=" + ",".join(spec.byday))
    if spec.until is not None:
        parts.append("UNTIL=" + spec.until.strftime("%Y%m%d"))
    if spec.count is not None:
        parts.append(f"COUNT={spec.count}")
    return "RRULE:" + ";".join(parts)


def exdate_line(day: date) -> str:
    return "EXDATE;VALUE=DATE:" + day.strftime("%Y%m%d")


def is_recurring(recurrence: Sequence[str] | None) -> bool:
    if not recurrence:
        return False
    return any(str(line).upper().startswith("RRULE") for line in recurrence)


def validate_lines(recurrence: Iterable[str]) -> tuple[str, ...]:
    """Limpia y valida las líneas (RRULE/EXDATE/RDATE/EXRULE). Lanza `RecurrenceError`."""
    out: list[str] = []
    for raw in recurrence:
        line = str(raw).strip()
        if not line:
            continue
        if not _RULE_LINE_RE.match(line):
            raise RecurrenceError(f"línea de recurrencia inválida: {line!r}")
        out.append(line)
    if out:
        try:
            rrulestr("\n".join(out), dtstart=datetime(2000, 1, 1), forceset=True)
        except (ValueError, TypeError, KeyError) as e:
            raise RecurrenceError(f"regla inválida ({e})") from e
    return tuple(out)


def _dtstart(starts_on: date, start_time: time | None) -> datetime:
    return datetime.combine(starts_on, start_time or time(0, 0))


_UNTIL_UTC_RE = re.compile(r"UNTIL=(\d{8}T\d{6})Z")
_UNTIL_DATE_RE = re.compile(r"UNTIL=(\d{8})(?![T\d])")


def until_local(line: str) -> str:
    """Normaliza el `UNTIL` de una línea RRULE a la HORA LOCAL naive con la que memex guarda las
    fechas (decisión 0010). Google escribe `UNTIL=20260926T045959Z` (UTC, RFC 5545): con un DTSTART
    naive dateutil lo rechaza («UNTIL values must be specified in UTC when DTSTART is
    timezone-aware»), y además `describe` mostraría el día UTC (el 26 en vez del 25). Se pasa a la
    zona local y se quita la zona. Sin UNTIL-UTC la línea sale tal cual."""
    m = _UNTIL_UTC_RE.search(line)
    if m is None:
        return line
    try:
        utc_dt = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return line
    local = utc_dt.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return line.replace(m.group(0), "UNTIL=" + local.strftime("%Y%m%dT%H%M%S"))


def _ruleset(starts_on: date, start_time: time | None, recurrence: Sequence[str]) -> Any:
    # UNTIL solo-fecha (YYYYMMDD) termina el día entero: dateutil lo lee como medianoche, así que
    # con hora de inicio > 00:00 la última ocurrencia se perdería. Se estira al final de ese día.
    # UNTIL en UTC (Google) se pasa a hora local naive (`until_local`), como el DTSTART.
    lines: list[str] = []
    for line in recurrence:
        line = until_local(str(line))
        m = _UNTIL_DATE_RE.search(line)
        if m and start_time is not None:
            line = line.replace(m.group(0), f"UNTIL={m.group(1)}T235959")
        lines.append(line)
    return rrulestr("\n".join(lines), dtstart=_dtstart(starts_on, start_time), forceset=True)


def occurrences(
    starts_on: date,
    recurrence: Sequence[str],
    *,
    window_start: date,
    window_end: date,
    start_time: time | None = None,
    exclude: Iterable[date] = (),
) -> list[date]:
    """Fechas de las ocurrencias del maestro dentro de `[window_start, window_end]` (inclusive),
    aplicando EXDATE y `exclude` (ocurrencias reemplazadas por excepciones). Sin recurrencia →
    `[starts_on]` si cae en la ventana. Acotado por `MAX_OCCURRENCES`."""
    if window_end < window_start:
        return []
    if not is_recurring(recurrence):
        return [starts_on] if window_start <= starts_on <= window_end else []
    rules = _ruleset(starts_on, start_time, recurrence)
    lo = datetime.combine(window_start, time(0, 0))
    hi = datetime.combine(window_end, time(23, 59, 59))
    skip = set(exclude)
    out: list[date] = []
    for dt in rules.between(lo, hi, inc=True):
        d = dt.date()
        if d in skip:
            continue
        out.append(d)
        if len(out) >= MAX_OCCURRENCES:
            break
    return out


def occurs_on(
    starts_on: date, recurrence: Sequence[str], day: date, *, start_time: time | None = None
) -> bool:
    return bool(
        occurrences(starts_on, recurrence, window_start=day, window_end=day, start_time=start_time)
    )


def next_occurrences(
    starts_on: date,
    recurrence: Sequence[str],
    *,
    after: date,
    limit: int = 5,
    start_time: time | None = None,
    horizon_days: int = 400,
) -> list[date]:
    days = occurrences(
        starts_on,
        recurrence,
        window_start=after,
        window_end=after + timedelta(days=horizon_days),
        start_time=start_time,
    )
    return days[:limit]


def is_unbounded(recurrence: Sequence[str]) -> bool:
    """Sin UNTIL ni COUNT en la RRULE → la serie no termina (Google lo permite; memex también)."""
    for line in recurrence:
        up = str(line).upper()
        if up.startswith("RRULE") and "UNTIL=" not in up and "COUNT=" not in up:
            return True
    return False


#: Horizonte para buscar la última ocurrencia de una serie acotada (COUNT/UNTIL lejanos).
_LAST_OCCURRENCE_HORIZON_DAYS = 366 * 10


def last_occurrence(
    starts_on: date, recurrence: Sequence[str], *, start_time: time | None = None
) -> date | None:
    """Fecha de la ÚLTIMA ocurrencia: `starts_on` si no hay recurrencia, None si la serie no
    termina (`is_unbounded`), si no la última dentro de un horizonte de 10 años (tope práctico;
    más allá se devuelve la última que entró)."""
    if not is_recurring(recurrence):
        return starts_on
    if is_unbounded(recurrence):
        return None
    days = occurrences(
        starts_on,
        recurrence,
        window_start=starts_on,
        window_end=starts_on + timedelta(days=_LAST_OCCURRENCE_HORIZON_DAYS),
        start_time=start_time,
    )
    return days[-1] if days else starts_on


def describe(recurrence: Sequence[str]) -> str:
    """Texto corto en español para humanos: «semanal (lun, mié) hasta 2026-09-25»."""
    rule = next((str(x) for x in recurrence if str(x).upper().startswith("RRULE")), None)
    if rule is None:
        return ""
    params = dict(p.split("=", 1) for p in rule.split(":", 1)[1].split(";") if "=" in p)
    freq = _FREQ_NAMES_ES.get(params.get("FREQ", "").upper(), params.get("FREQ", "").lower())
    txt = freq
    interval = params.get("INTERVAL")
    if interval and interval != "1":
        txt += f" cada {interval}"
    byday = params.get("BYDAY")
    if byday:
        days = [_DAY_NAMES_ES.get(d.strip().upper()[-2:], d) for d in byday.split(",")]
        if days == ["lun", "mar", "mié", "jue", "vie"]:
            txt += " (lun a vie)"
        else:
            txt += " (" + ", ".join(days) + ")"
    until = params.get("UNTIL")
    if until:
        until = until_local(f"UNTIL={until}")[len("UNTIL=") :]  # día LOCAL, no el UTC de Google
        txt += f" hasta {until[:4]}-{until[4:6]}-{until[6:8]}"
    count = params.get("COUNT")
    if count:
        txt += f", {count} veces"
    ex = sum(1 for x in recurrence if str(x).upper().startswith("EXDATE"))
    if ex:
        txt += f" ({ex} excepción{'es' if ex > 1 else ''})"
    return txt


def exdates(recurrence: Sequence[str]) -> set[date]:
    out: set[date] = set()
    for line in recurrence:
        up = str(line).upper()
        if not up.startswith("EXDATE"):
            continue
        for token in up.split(":", 1)[1].split(","):
            token = token.strip()
            if len(token) >= 8 and token[:8].isdigit():
                out.add(date(int(token[:4]), int(token[4:6]), int(token[6:8])))
    return out


def expand_rows(
    rows: Sequence[dict[str, Any]],
    *,
    window_start: date,
    window_end: date,
) -> list[tuple[dict[str, Any], date]]:
    """Expande filas CONSOLIDADAS (dicts con `starts_on`, `ends_on`, `start_time`, `recurrence`,
    `series_id`, `original_start_on`) a pares `(fila, día)` dentro de la ventana:
    - un evento único entra si solapa la ventana (su día = `starts_on`);
    - un maestro recurrente entra una vez por ocurrencia, salvo las ocurrencias que una EXCEPCIÓN
      de su misma serie reemplaza (`original_start_on`);
    - una excepción entra por su propia fecha (`starts_on`) si cae en la ventana.
    El orden lo decide el caller."""
    overridden: dict[str, set[date]] = {}
    for r in rows:
        if r.get("original_start_on") is not None and r.get("series_id"):
            overridden.setdefault(str(r["series_id"]), set()).add(r["original_start_on"])
    out: list[tuple[dict[str, Any], date]] = []
    for r in rows:
        rec = tuple(r.get("recurrence") or ())
        if is_recurring(rec):
            skip = overridden.get(str(r.get("series_id") or ""), set())
            for day in occurrences(
                r["starts_on"],
                rec,
                window_start=window_start,
                window_end=window_end,
                start_time=r.get("start_time"),
                exclude=skip,
            ):
                out.append((r, day))
            continue
        last = r.get("ends_on") or r["starts_on"]
        if last >= window_start and r["starts_on"] <= window_end:
            out.append((r, r["starts_on"]))
    return out


def split_rrule(
    recurrence: Sequence[str], split_on: date
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """«Esta y las siguientes» (Google): parte la regla en `split_on`. Devuelve (líneas del
    maestro VIEJO, líneas del maestro NUEVO):
    - viejo: la RRULE con `UNTIL` = el día anterior (se quita COUNT) + sus EXDATE anteriores;
    - nuevo: la RRULE original sin COUNT (conserva su UNTIL si lo tenía) + los EXDATE desde
      `split_on`. El nuevo maestro arranca en `split_on` (lo fija el caller en `starts_on`)."""
    rule = next((str(x) for x in recurrence if str(x).upper().startswith("RRULE")), None)
    if rule is None:
        raise RecurrenceError("la serie no tiene RRULE")
    params = [
        p for p in rule.split(":", 1)[1].split(";") if p and not p.upper().startswith("COUNT=")
    ]
    without_until = [p for p in params if not p.upper().startswith("UNTIL=")]
    old_rule = "RRULE:" + ";".join(
        [*without_until, "UNTIL=" + (split_on - timedelta(days=1)).strftime("%Y%m%d")]
    )
    new_rule = "RRULE:" + ";".join(params)
    old_ex = [exdate_line(d) for d in sorted(exdates(recurrence)) if d < split_on]
    new_ex = [exdate_line(d) for d in sorted(exdates(recurrence)) if d >= split_on]
    return (old_rule, *old_ex), (new_rule, *new_ex)
