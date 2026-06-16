"""Lote de procesamiento por ventanas de cantidad: estado persistido + avance ventana a ventana.

Espejo de `memex.backfill.service` pero sobre lo YA ingerido: el lote congela un snapshot de
inbox_ids (`select_targets`, orden cronológico) y se avanza en ventanas de N mensajes vía
`reprocess()`. La frontera es un ÍNDICE dentro del snapshot y se persiste SOLO si la ventana
terminó sin falla dura de etapa — una corrida interrumpida (quota agotada, DB caída) se retoma
sobre la misma ventana, que es re-ejecutable (la idempotencia por cursor evita re-gastar lo ya
hecho salvo `force`).

El tamaño de ventana tiene default POR MEDIO (`processing_window_defaults`): chat es denso y
barato por mensaje, correo es largo. Al crear el lote se resuelve con el `min()` de los defaults
de los kinds presentes en el snapshot (conservador en gasto); cada avance puede pisarlo y el
override queda como nuevo default del lote (igual que el backfill de ingesta).

`run_advance` corre EN BACKGROUND dentro del API (mismo patrón que `/processing/run`): una
llamada = una fila `worker_runs` (`run_type='reprocess'`), tanto para UNA ventana como para "el
resto" (que loopea ventana a ventana, persistiendo el avance después de cada una para que el
progreso sea visible y reanudable).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Connection, text

from memex.core.source import SourceKind
from memex.db import connection
from memex.llm import LLMQuotaError
from memex.logging import bound_log_context, get_logger
from memex.relevance.mining import run_rule_mining
from memex.relevance.settings import get_settings
from memex.reprocess import reprocess
from memex.scheduler import runs

_log = get_logger("memex.processing.lots")

_NO_LOT = "no hay lote de procesamiento configurado"

#: Tamaño de ventana default por medio (mensajes por avance). Editables por usuario
#: (`processing_window_defaults`); esto es el piso cuando nunca tocó la perilla.
DEFAULT_WINDOW_BY_KIND: dict[str, int] = {
    SourceKind.EMAIL.value: 50,
    SourceKind.CHAT.value: 200,
    SourceKind.SOCIAL.value: 100,
}
#: Default para un kind nuevo sin entrada en la tabla NI en el dict de arriba.
_FALLBACK_WINDOW = 50


def known_kinds() -> list[str]:
    """Los medios válidos para defaults de ventana (el enum canónico, no una copia)."""
    return [k.value for k in SourceKind]


@dataclass
class ProcessingLot:
    """Fila de `processing_lots` materializada. `frontier` = mensajes YA procesados (índice)."""

    user_id: int
    stages: list[str]
    config: dict[str, Any]  # {"filters": {...}, "force": bool} (eco de la creación, para la UI)
    target_ids: list[int]
    frontier: int
    window_size: int
    status: str
    history: list[dict[str, Any]]
    created_at: datetime


_SELECT = """
    SELECT user_id, stages, config, target_ids, frontier, window_size, status, history, created_at
    FROM processing_lots
    WHERE user_id = :uid
"""


def _row_to_lot(row: Any) -> ProcessingLot:
    return ProcessingLot(
        user_id=int(row["user_id"]),
        stages=list(row["stages"]),
        config=dict(row["config"] or {}),
        target_ids=[int(i) for i in row["target_ids"]],
        frontier=int(row["frontier"]),
        window_size=int(row["window_size"]),
        status=str(row["status"]),
        history=list(row["history"] or []),
        created_at=row["created_at"],
    )


def get_lot(conn: Connection, user_id: int) -> ProcessingLot | None:
    row = conn.execute(text(_SELECT), {"uid": user_id}).mappings().first()
    return _row_to_lot(row) if row is not None else None


def is_busy(conn: Connection, user_id: int) -> bool:
    """True si hay una corrida de procesamiento (`run_type='reprocess'`) en curso del user."""
    return (
        conn.execute(
            text(
                "SELECT 1 FROM worker_runs "
                "WHERE user_id = :uid AND run_type = 'reprocess' AND status = 'running' LIMIT 1"
            ),
            {"uid": user_id},
        ).first()
        is not None
    )


# --- defaults de ventana por medio ---
def window_defaults(conn: Connection, user_id: int) -> dict[str, int]:
    """Tamaño de ventana por medio: lo guardado por el user sobre los defaults del código."""
    sizes = {k: DEFAULT_WINDOW_BY_KIND.get(k, _FALLBACK_WINDOW) for k in known_kinds()}
    rows = conn.execute(
        text("SELECT kind, window_size FROM processing_window_defaults WHERE user_id = :uid"),
        {"uid": user_id},
    ).all()
    for kind, size in rows:
        if str(kind) in sizes:  # un kind retirado del enum no debe reaparecer en la UI
            sizes[str(kind)] = int(size)
    return sizes


def set_window_defaults(conn: Connection, user_id: int, sizes: dict[str, int]) -> dict[str, int]:
    """UPSERT de los defaults por medio. Valida kinds contra el enum y tamaños >= 1."""
    valid = set(known_kinds())
    bad = sorted(k for k in sizes if k not in valid)
    if bad:
        raise HTTPException(
            status_code=422, detail=f"medios desconocidos: {bad}; válidos: {sorted(valid)}"
        )
    for kind, size in sizes.items():
        if size < 1:
            raise HTTPException(status_code=422, detail=f"window_size de {kind!r} debe ser >= 1")
        conn.execute(
            text(
                """
                INSERT INTO processing_window_defaults (user_id, kind, window_size)
                VALUES (:uid, :kind, :size)
                ON CONFLICT (user_id, kind) DO UPDATE
                    SET window_size = EXCLUDED.window_size, updated_at = NOW()
                """
            ),
            {"uid": user_id, "kind": kind, "size": size},
        )
    return window_defaults(conn, user_id)


def _kinds_of_targets(conn: Connection, target_ids: list[int]) -> set[str]:
    """Kinds (email/chat/social) presentes en el snapshot, vía el tipo de la fuente."""
    from memex.sources import kind_for_type  # import local: evita ciclos al importar

    rows = conn.execute(
        text(
            "SELECT DISTINCT s.type FROM inbox i JOIN sources s ON s.id = i.source_id "
            "WHERE i.id = ANY(:iids)"
        ),
        {"iids": target_ids},
    ).all()
    kinds: set[str] = set()
    for (source_type,) in rows:
        try:
            kinds.add(kind_for_type(str(source_type)).value)
        except KeyError:  # tipo sin kind registrado: no aporta default, no rompe el lote
            continue
    return kinds


def resolve_window_size(
    conn: Connection, user_id: int, target_ids: list[int], explicit: int | None
) -> int:
    """Tamaño de ventana del lote: explícito > min() de los defaults de los kinds presentes.

    El `min()` es deliberadamente conservador en gasto cuando el lote mezcla medios ("todas las
    fuentes"); con una sola fuente equivale al default de su medio.
    """
    if explicit is not None:
        return explicit
    defaults = window_defaults(conn, user_id)
    kinds = _kinds_of_targets(conn, target_ids)
    if not kinds:
        return _FALLBACK_WINDOW
    return min(defaults.get(k, _FALLBACK_WINDOW) for k in kinds)


# --- alta / baja ---
def upsert_lot(
    conn: Connection,
    user_id: int,
    *,
    stages: list[str],
    target_ids: list[int],
    filters: dict[str, Any],
    force: bool,
    window_size: int,
) -> ProcessingLot:
    """Crea o reconfigura EL lote del user (uno solo): frontera 0, history vacío, activo."""
    conn.execute(
        text(
            """
            INSERT INTO processing_lots
              (user_id, stages, config, target_ids, frontier, window_size, status, history)
            VALUES
              (:uid, :stages, CAST(:config AS JSONB), :tids, 0, :ws, 'active', '[]'::jsonb)
            ON CONFLICT (user_id) DO UPDATE SET
              stages      = EXCLUDED.stages,
              config      = EXCLUDED.config,
              target_ids  = EXCLUDED.target_ids,
              frontier    = 0,
              window_size = EXCLUDED.window_size,
              status      = 'active',
              history     = '[]'::jsonb,
              updated_at  = NOW()
            """
        ),
        {
            "uid": user_id,
            "stages": stages,
            "config": json.dumps({"filters": filters, "force": force}),
            "tids": target_ids,
            "ws": window_size,
        },
    )
    lot = get_lot(conn, user_id)
    assert lot is not None
    return lot


def delete_lot(conn: Connection, user_id: int) -> bool:
    res = conn.execute(text("DELETE FROM processing_lots WHERE user_id = :uid"), {"uid": user_id})
    return bool(res.rowcount)


def to_state(conn: Connection, lot: ProcessingLot) -> dict[str, Any]:
    """Estado para la UI: progreso + gasto acumulado + busy + defaults por medio."""
    spent = round(sum(float(h.get("cost_usd", 0) or 0) for h in lot.history), 6)
    return {
        "stages": lot.stages,
        "filters": lot.config.get("filters", {}),
        "force": bool(lot.config.get("force", False)),
        "total": len(lot.target_ids),
        "frontier": lot.frontier,
        "window_size": lot.window_size,
        "status": lot.status,
        "spent_usd": spent,
        "busy": is_busy(conn, lot.user_id),
        "defaults": window_defaults(conn, lot.user_id),
        "history": lot.history,
        "created_at": lot.created_at,
    }


# --- avance ---
def _stage_hard_failure(results: dict[str, Any]) -> str | None:
    """Nombre de la primera etapa con falla DURA (`{"error": ...}`), o None.

    Distinto de `errors > 0` (fallas por-mensaje, que avanzan igual y quedan re-apuntables con el
    filtro `only=errored`): una falla dura (quota agotada, DB caída) invalida la ventana entera.
    """
    for stage, slot in results.items():
        if isinstance(slot, dict) and "error" in slot:
            return stage
    return None


def _persist_advance(
    conn: Connection,
    user_id: int,
    *,
    new_frontier: int,
    window_size: int,
    entry: dict[str, Any],
) -> ProcessingLot:
    """Mueve la frontera, appendea la ventana al history y guarda el tamaño como nuevo default."""
    row = (
        conn.execute(
            text("SELECT target_ids, history FROM processing_lots WHERE user_id = :uid FOR UPDATE"),
            {"uid": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail=_NO_LOT)
    history = list(row["history"] or [])
    history.append(entry)
    status = "done" if new_frontier >= len(row["target_ids"]) else "active"
    conn.execute(
        text(
            """
            UPDATE processing_lots SET
              frontier    = :f,
              window_size = :ws,
              status      = :s,
              history     = CAST(:h AS JSONB),
              updated_at  = NOW()
            WHERE user_id = :uid
            """
        ),
        {
            "f": new_frontier,
            "ws": window_size,
            "s": status,
            "h": json.dumps(history),
            "uid": user_id,
        },
    )
    lot = get_lot(conn, user_id)
    assert lot is not None
    return lot


async def _mine_between_windows(user_id: int, lot: ProcessingLot) -> dict[str, Any] | None:
    """Minería de reglas INTERCALADA entre ventanas del lote (procesamiento incremental).

    El corazón del modelo incremental: tras juzgar una ventana, se mina sobre el ACUMULADO de
    no-relevantes — cuando una clase (dominio) cruza el umbral, se proponen reglas (dry-run →
    auto-activa) y la ventana SIGUIENTE corto-circuita esa clase GRATIS, sin volver al juez. NO
    es "juzgar todo y después minar": el sistema aprende sobre la marcha y cada ventana sale más
    barata.

    Solo con el gate encendido, `relevance` en las etapas del lote y `mining_interleave` ON. La
    minería es umbral-gated (no-op barato sin LLM hasta acumular). Best-effort salvo quota: una
    falla de minería NO frena el lote; `LLMQuotaError` sí (saldo agotado).
    """
    if "relevance" not in lot.stages:
        return None
    with connection() as conn:
        settings = get_settings(conn, user_id)
    if not settings.enabled or not settings.mining_interleave:
        return None
    try:
        stats = await run_rule_mining(user_id)
    except LLMQuotaError:
        raise
    except Exception as e:  # best-effort: minar no debe tumbar el avance del lote
        _log.warning("processing.lot.mining_failed", error=str(e), exc_type=type(e).__name__)
        return None
    if stats.proposed == 0 and stats.activated == 0:
        return None  # por debajo del umbral / sin patrones → sin ruido en el history
    return {
        "senders": stats.senders,
        "proposed": stats.proposed,
        "activated": stats.activated,
        "rejected": stats.rejected,
        "cost_usd": float(stats.cost.total.cost_usd),
    }


async def run_advance(
    user_id: int, run_id: int, *, rest: bool, window_size: int | None = None
) -> None:
    """Avanza el lote EN BACKGROUND: una ventana (`rest=False`) o todas las que queden.

    Cada ventana = un `reprocess()` sobre los próximos N ids del snapshot; el avance (frontera +
    history con costo) se persiste DESPUÉS de cada ventana, así "el resto" muestra progreso vivo
    y un corte a mitad retoma donde quedó. Una falla dura de etapa corta el loop SIN avanzar esa
    ventana y cierra la corrida en error. Cierra la fila `worker_runs` siempre (ok/error).

    El run_id va por CONTEXTVARS (no `.bind()` local): cada `reprocess()` de ventana — y todo lo
    que corre adentro — hereda la correlación, y `/logs?run_id=` reconstruye la corrida entera.
    """
    totals = {"windows": 0, "targets": 0, "cost_usd": 0.0}
    with bound_log_context(run_id=str(run_id), user_id=user_id):
        try:
            while True:
                with connection() as conn:
                    lot = get_lot(conn, user_id)
                if lot is None:
                    runs.finish_run(run_id, status="error", error="el lote fue borrado a mitad")
                    _log.warning("processing.lot.vanished")
                    return
                remaining = len(lot.target_ids) - lot.frontier
                if remaining <= 0:
                    break
                size = window_size or lot.window_size
                ids = lot.target_ids[lot.frontier : lot.frontier + size]

                t0 = time.monotonic()
                result = await reprocess(
                    user_id,
                    stages=lot.stages,
                    targets=ids,
                    force=bool(lot.config.get("force", False)),
                )
                ms = int((time.monotonic() - t0) * 1000)
                results = dict(result.get("results", {}))

                failed_stage = _stage_hard_failure(results)
                if failed_stage is not None:
                    detail = str(results[failed_stage].get("error", ""))[:500]
                    runs.finish_run(
                        run_id,
                        status="error",
                        stats={**totals, "results": results},
                        error=f"etapa {failed_stage!r} falló en la ventana {lot.frontier}"
                        f"-{lot.frontier + len(ids)}: {detail}",
                    )
                    _log.error(
                        "processing.lot.window_failed",
                        stage=failed_stage,
                        start_idx=lot.frontier,
                        n=len(ids),
                    )
                    return

                # Minería intercalada: tras juzgar la ventana, destila reglas del acumulado para
                # que la SIGUIENTE corto-circuite gratis (incremental). Corre antes de persistir
                # para que su resultado quede en el history de la ventana.
                mining = await _mine_between_windows(user_id, lot)
                mining_cost = float(mining["cost_usd"]) if mining else 0.0

                entry: dict[str, Any] = {
                    "start_idx": lot.frontier,
                    "end_idx": lot.frontier + len(ids),
                    "n": len(ids),
                    "results": results,
                    "errors": sum(
                        int(slot.get("errors", 0) or 0)
                        for slot in results.values()
                        if isinstance(slot, dict)
                    ),
                    "cost_usd": float(result.get("cost_usd", 0) or 0),
                    "mining": mining,
                    "ms_elapsed": ms,
                    "at": datetime.now(UTC).isoformat(),
                }
                with connection() as conn:
                    lot = _persist_advance(
                        conn,
                        user_id,
                        new_frontier=lot.frontier + len(ids),
                        window_size=size,
                        entry=entry,
                    )
                totals["windows"] += 1
                totals["targets"] += len(ids)
                totals["cost_usd"] = round(totals["cost_usd"] + entry["cost_usd"] + mining_cost, 6)
                _log.info(
                    "processing.lot.window_done",
                    start_idx=entry["start_idx"],
                    n=entry["n"],
                    cost_usd=entry["cost_usd"],
                    status=lot.status,
                )
                if not rest or lot.status == "done":
                    break

            runs.finish_run(run_id, status="ok", stats=totals)
            _log.info("processing.lot.advance_done", **totals)
        except Exception as e:  # best-effort: el error queda en la fila para el post-mortem
            runs.finish_run(run_id, status="error", stats=totals, error=str(e))
            _log.error("processing.lot.advance_failed", error=str(e), exc_type=type(e).__name__)
