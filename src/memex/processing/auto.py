"""Procesamiento automático de lo NUEVO (job `procesar_nuevos` del scheduler).

En dev el procesamiento era manual: un mensaje del grupo del curso entraba al inbox (WhatsApp en
vivo; Telegram, correo y Notion por traída) y no generaba tareas, datos ni hitos hasta que alguien
corría el lote. Este job cierra ese hueco de forma ACOTADA y con costo visible:

- solo los MEDIOS elegidos (`kinds`; default chat y notas: los chats ya son solo los elegidos en
  /cuenta, los documentos se procesan al subirlos);
- cada medio lleva SU marca de agua (`watermarks`, sobre `inbox.received_at`) y su cuota de la
  corrida. Con una marca compartida, sumar un medio nuevo lo dejaba mudo: el correo entró cuando la
  marca ya había avanzado con el tráfico de los chats y 158 correos viejos nunca se miraron;
- todas las etapas del pipeline (`reprocess`: media, ocr, classify, relevance, extract, asuntos,
  graph), idempotentes por cursor: repetir no re-gasta;
- con TOPE DIARIO de gasto (`daily_cost_cap_usd`, contra `llm_calls` desde la medianoche local):
  al tocarlo la corrida se salta y queda anotado (`last_skip_reason`).

Cada corrida es una fila `worker_runs` (job `procesar_nuevos`) con stats y costo (lo escribe el
daemon); el rastro corto vive en `auto_processing` (GET/PATCH /processing/auto).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.localtime import LOCAL_TZ
from memex.core.source import SourceKind
from memex.db import connection
from memex.logging import get_logger
from memex.reprocess import STAGE_ORDER, reprocess
from memex.sources import kind_for_type, kind_types

_log = get_logger("memex.processing.auto")

JOB_NAME = "procesar_nuevos"
DEFAULT_KINDS: tuple[str, ...] = (SourceKind.CHAT.value, SourceKind.NOTES.value)
#: Sin marca de agua (medio recién sumado) se mira desde hace un día, no desde el principio.
FIRST_RUN_LOOKBACK = timedelta(days=1)

ReprocessFn = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class AutoSettings:
    enabled: bool
    kinds: tuple[str, ...]
    #: Hasta dónde se miró CADA medio (`received_at`). Un medio sin entrada arranca de cero.
    watermarks: dict[str, datetime]
    max_per_run: int
    daily_cost_cap_usd: Decimal
    last_run_at: datetime | None
    last_targets: int
    last_cost_usd: Decimal
    last_skip_reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "kinds": list(self.kinds),
            "watermarks": dict(self.watermarks),
            "max_per_run": self.max_per_run,
            "daily_cost_cap_usd": float(self.daily_cost_cap_usd),
            "last_run_at": self.last_run_at,
            "last_targets": self.last_targets,
            "last_cost_usd": float(self.last_cost_usd),
            "last_skip_reason": self.last_skip_reason,
        }


@dataclass
class AutoStats:
    """Resultado de una corrida (va a `worker_runs.stats`)."""

    enabled: bool = True
    targets: int = 0
    cost_usd: float = 0.0
    spent_today_usd: float = 0.0
    skipped: str = ""  #: '' | apagado | tope_diario | sin_novedades
    #: Cuántos mensajes entraron por medio y hasta dónde quedó cada marca.
    by_kind: dict[str, int] = field(default_factory=dict)
    watermarks: dict[str, str] = field(default_factory=dict)
    kinds: list[str] = field(default_factory=list)
    stages: dict[str, Any] = field(default_factory=dict)


_COLS = (
    "enabled, kinds, watermarks, max_per_run, daily_cost_cap_usd, last_run_at, last_targets, "
    "last_cost_usd, last_skip_reason"
)


def _parse_watermarks(raw: Any) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    for k, v in dict(raw or {}).items():
        if not v:
            continue
        ts = datetime.fromisoformat(str(v)) if isinstance(v, str) else v
        out[str(k)] = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    return out


def _row(r: Any) -> AutoSettings:
    return AutoSettings(
        enabled=bool(r["enabled"]),
        kinds=tuple(str(k) for k in (r["kinds"] or [])),
        watermarks=_parse_watermarks(r["watermarks"]),
        max_per_run=int(r["max_per_run"]),
        daily_cost_cap_usd=Decimal(str(r["daily_cost_cap_usd"])),
        last_run_at=r["last_run_at"],
        last_targets=int(r["last_targets"] or 0),
        last_cost_usd=Decimal(str(r["last_cost_usd"] or 0)),
        last_skip_reason=str(r["last_skip_reason"] or ""),
    )


def get_settings(conn: Connection, user_id: int) -> AutoSettings:
    """Los ajustes del user; la fila nace con los defaults la primera vez que se pide."""
    row = (
        conn.execute(
            text(f"SELECT {_COLS} FROM auto_processing WHERE user_id = :u"), {"u": user_id}
        )
        .mappings()
        .first()
    )
    if row is None:
        row = (
            conn.execute(
                text(
                    "INSERT INTO auto_processing (user_id) VALUES (:u) "
                    "ON CONFLICT (user_id) DO UPDATE SET user_id = EXCLUDED.user_id "
                    f"RETURNING {_COLS}"
                ),
                {"u": user_id},
            )
            .mappings()
            .one()
        )
    return _row(row)


def known_kinds() -> list[str]:
    return [k.value for k in SourceKind]


def types_for_kinds(kinds: Sequence[str]) -> list[str]:
    """Tipos de fuente (telegram, whatsapp, imap…) que caen en esos medios."""
    wanted = set(kinds)
    return [t for t in kind_types() if kind_for_type(t).value in wanted]


def update_settings(
    conn: Connection,
    user_id: int,
    *,
    enabled: bool | None = None,
    kinds: Sequence[str] | None = None,
    max_per_run: int | None = None,
    daily_cost_cap_usd: float | Decimal | None = None,
    watermarks: Mapping[str, datetime | None] | None = None,
    last_run: dict[str, Any] | None = None,
) -> AutoSettings:
    """Cambia lo que venga. `watermarks` es un parche por medio: un valor mueve esa marca (volver a
    mirar desde ahí), `None` la borra (ese medio vuelve a arrancar desde su ventana). `last_run` =
    rastro de la corrida."""
    current = get_settings(conn, user_id)
    sets: dict[str, Any] = {}
    if enabled is not None:
        sets["enabled"] = enabled
    if kinds is not None:
        bad = [k for k in kinds if k not in known_kinds()]
        if bad:
            raise ValueError(f"medios inválidos: {bad}; válidos: {known_kinds()}")
        sets["kinds"] = list(dict.fromkeys(kinds))
    if max_per_run is not None:
        if max_per_run < 1:
            raise ValueError("max_per_run debe ser >= 1")
        sets["max_per_run"] = int(max_per_run)
    if daily_cost_cap_usd is not None:
        if Decimal(str(daily_cost_cap_usd)) < 0:
            raise ValueError("daily_cost_cap_usd no puede ser negativo")
        sets["daily_cost_cap_usd"] = Decimal(str(daily_cost_cap_usd))
    if watermarks is not None:
        bad = [k for k in watermarks if k not in known_kinds()]
        if bad:
            raise ValueError(f"medios inválidos: {bad}; válidos: {known_kinds()}")
        merged = dict(current.watermarks)
        for k, v in watermarks.items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        sets["watermarks"] = _dump_watermarks(merged)
    for key in ("last_run_at", "last_targets", "last_cost_usd", "last_skip_reason"):
        if last_run and key in last_run:
            sets[key] = last_run[key]
    if sets:
        assigns = ", ".join(
            f"{k} = CAST(:{k} AS JSONB)" if k == "watermarks" else f"{k} = :{k}" for k in sets
        )
        conn.execute(
            text(f"UPDATE auto_processing SET {assigns}, updated_at = NOW() WHERE user_id = :u"),
            {**sets, "u": user_id},
        )
    return get_settings(conn, user_id)


def _dump_watermarks(w: Mapping[str, datetime]) -> str:
    import json

    return json.dumps({k: v.isoformat() for k, v in w.items()})


def cost_today_usd(conn: Connection, user_id: int, now: datetime | None = None) -> Decimal:
    """Gasto en LLM desde la medianoche LOCAL (todas las corridas, no solo este job: el tope es
    del día de la persona)."""
    now = now or datetime.now(UTC)
    midnight = now.astimezone(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    total = conn.execute(
        text(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls "
            "WHERE user_id = :u AND created_at >= :since"
        ),
        {"u": user_id, "since": midnight.astimezone(UTC)},
    ).scalar()
    return Decimal(str(total or 0))


def select_new(
    conn: Connection,
    user_id: int,
    *,
    types: Sequence[str],
    since: datetime,
    limit: int,
) -> list[tuple[int, datetime]]:
    """Lo recibido después de `since` en fuentes de esos tipos, en orden de llegada."""
    if not types:
        return []
    rows = conn.execute(
        text(
            """
            SELECT i.id, i.received_at
            FROM inbox i JOIN sources s ON s.id = i.source_id
            WHERE i.user_id = :u AND s.type = ANY(:types) AND i.received_at > :since
            ORDER BY i.received_at, i.id
            LIMIT :lim
            """
        ),
        {"u": user_id, "types": list(types), "since": since, "lim": limit},
    ).all()
    return [(int(r[0]), r[1]) for r in rows]


def _stage_summary(results: dict[str, Any]) -> dict[str, Any]:
    """Lo que vale la pena guardar por etapa: contadores y costo, no los detalles."""
    out: dict[str, Any] = {}
    for stage, res in (results or {}).items():
        if isinstance(res, dict):
            out[stage] = {
                k: v
                for k, v in res.items()
                if isinstance(v, (int, float, str)) and k != "error_class"
            }
        else:
            out[stage] = str(res)[:200]
    return out


async def run_auto_processing(
    user_id: int,
    *,
    reprocess_fn: ReprocessFn | None = None,
    now: datetime | None = None,
) -> AutoStats:
    """Una corrida: si está prendido y no se tocó el tope del día, procesa lo nuevo de cada medio
    elegido —con su marca y su cuota de la corrida— y avanza esas marcas. Una etapa con errores NO
    frena la marca (los errores quedan en la corrida y se reintentan con `--only errored`); una
    cuota agotada del proveedor sí (la excepción sube y el daemon marca la corrida fallida)."""
    now = now or datetime.now(UTC)
    stats = AutoStats()
    with connection() as conn:
        st = get_settings(conn, user_id)
        stats.enabled = st.enabled
        stats.kinds = list(st.kinds)
        if not st.enabled:
            stats.skipped = "apagado"
            update_settings(
                conn,
                user_id,
                last_run={"last_run_at": now, "last_targets": 0, "last_skip_reason": "apagado"},
            )
            return stats
        spent = cost_today_usd(conn, user_id, now)
        stats.spent_today_usd = float(spent)
        if spent >= st.daily_cost_cap_usd:
            stats.skipped = "tope_diario"
            update_settings(
                conn,
                user_id,
                last_run={"last_run_at": now, "last_targets": 0, "last_skip_reason": "tope_diario"},
            )
            _log.warning(
                "processing.auto.cap",
                user_id=user_id,
                spent_usd=float(spent),
                cap_usd=float(st.daily_cost_cap_usd),
            )
            return stats

        # Cada medio con su marca y su cuota: así un medio hablador no deja sin turno a los demás
        # ni arrastra la marca de uno callado.
        budget = max(1, st.max_per_run // max(1, len(st.kinds)))
        picked: dict[str, list[tuple[int, datetime]]] = {}
        starts: dict[str, datetime] = {}
        for kind in st.kinds:
            since = st.watermarks.get(kind) or (now - FIRST_RUN_LOOKBACK)
            starts[kind] = since
            rows = select_new(
                conn, user_id, types=types_for_kinds([kind]), since=since, limit=budget
            )
            if rows:
                picked[kind] = rows

        if not picked:
            stats.skipped = "sin_novedades"
            # Un medio que nunca miró nada fija su arranque: si no, la ventana se desliza sola.
            nuevos = {k: v for k, v in starts.items() if k not in st.watermarks}
            update_settings(
                conn,
                user_id,
                watermarks=nuevos or None,
                last_run={
                    "last_run_at": now,
                    "last_targets": 0,
                    "last_skip_reason": "sin_novedades",
                },
            )
            stats.watermarks = {k: v.isoformat() for k, v in {**starts, **st.watermarks}.items()}
            return stats

    ids = [i for rows in picked.values() for i, _ in rows]
    fn = reprocess_fn or reprocess
    result = await fn(user_id, stages=list(STAGE_ORDER), targets=ids)
    cost = float(result.get("cost_usd") or 0)
    avance = {k: max(ts for _, ts in rows) for k, rows in picked.items()}
    # Un medio sin novedades fija su arranque la primera vez que se lo mira: si no, su ventana se
    # desliza sola y un mensaje que llegó mientras el sistema estaba apagado se perdería.
    for kind, since in starts.items():
        avance.setdefault(kind, since)
    stats.targets = len(ids)
    stats.cost_usd = cost
    stats.spent_today_usd = float(spent) + cost
    stats.by_kind = {k: len(rows) for k, rows in picked.items()}
    stats.watermarks = {k: v.isoformat() for k, v in avance.items()}
    stats.stages = _stage_summary(result.get("results") or {})
    with connection() as conn:
        update_settings(
            conn,
            user_id,
            watermarks=avance,
            last_run={
                "last_run_at": now,
                "last_targets": len(ids),
                "last_cost_usd": Decimal(str(cost)),
                "last_skip_reason": "",
            },
        )
    _log.info(
        "processing.auto.done",
        user_id=user_id,
        targets=len(ids),
        by_kind=stats.by_kind,
        cost_usd=cost,
        spent_today_usd=stats.spent_today_usd,
        watermarks=stats.watermarks,
    )
    return stats


def stats_dict(stats: AutoStats) -> dict[str, Any]:
    return asdict(stats)
