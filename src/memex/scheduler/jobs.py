"""Definición de los jobs del scheduler + el registry nombre→job + el ciclo de calendar.

Un `Job` presenta una interfaz uniforme `async (user_id) -> stats|None` sobre los workers
server-side (algunos sync, otros async). Los SÍNCRONOS se corren en un thread aparte
(`asyncio.to_thread`) para no bloquear el event loop del daemon.

Todos los jobs se DEFINEN acá, pero ninguno corre por default: el scheduler arranca desarmado
(`SchedulerSettings.enabled_jobs` vacío). Ver `memex.scheduler.config.build_jobs`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from memex.classifier.worker import run_classification
from memex.db import connection
from memex.llm.client import LLMQuotaError
from memex.logging import get_logger
from memex.modules.calendar.consolidate import run_consolidation
from memex.modules.calendar.dedup_llm import run_dedup_phase2
from memex.modules.calendar.merge_llm import run_merge
from memex.modules.calendar.sync import run_pull, run_push
from memex.modules.identidades.dedup_llm import run_merge_phase2
from memex.modules.identidades.hierarchy import run_organize
from memex.modules.identidades.relations_llm import run_cooccurrence_llm
from memex.modules.identidades.sync import run_sync as run_identidades_sync
from memex.modules.orchestrator import run_extraction
from memex.ocr.worker import run_ocr
from memex.summarizer.worker import run_summarization

_log = get_logger("memex.scheduler.jobs")

#: Firma uniforme de un job: recibe el user_id y devuelve su stats (o None).
JobRun = Callable[[int], Awaitable[Any]]


@dataclass(frozen=True)
class Job:
    """Un trabajo agendable: nombre (literal grepeable), intervalo default ISO-8601 y la corrida."""

    name: str
    default_interval: str
    run: JobRun


def _sync(fn: Callable[[int], Any]) -> JobRun:
    """Adapta un worker SÍNCRONO a la firma async del Job, corriéndolo fuera del event loop."""

    async def _run(user_id: int) -> Any:
        return await asyncio.to_thread(fn, user_id)

    return _run


@dataclass
class LogPurgeStats:
    """Roll-up del purge de `log_events`: filas borradas + salud del sink al momento del corte."""

    deleted: int = 0
    sink_dropped: int = 0
    sink_db_errors: int = 0


def run_log_purge(user_id: int) -> LogPurgeStats:
    """Borra los `log_events` más viejos que la retención y reporta la salud del sink.

    Retención global (no por user): el sink persiste líneas de TODOS (incl. pre-auth/infra), así
    que el DELETE no filtra por `user_id` — el parámetro existe solo para cumplir la firma del Job.
    """
    from memex.config import settings  # import local: evita el ciclo config↔jobs al importar
    from memex.core.log_sink import sink_health

    retention_days = settings.log_persist_retention_days
    with connection() as conn:
        res = conn.execute(
            text("DELETE FROM log_events WHERE ts < NOW() - make_interval(days => :d)"),
            {"d": retention_days},
        )
    deleted = res.rowcount or 0
    health = sink_health()
    _log.info(
        "log_purge.done",
        deleted=deleted,
        retention_days=retention_days,
        sink_dropped=health["dropped"],
        sink_db_errors=health["db_errors"],
    )
    return LogPurgeStats(
        deleted=deleted,
        sink_dropped=health["dropped"],
        sink_db_errors=health["db_errors"],
    )


@dataclass
class CalendarCycleStats:
    """Roll-up de un ciclo completo de calendar (pull→dedup→consolidate→merge→push).

    OJO: `errors`/`steps_failed` son agregados de VISIBILIDAD del ciclo y SOLAPAN con los
    contadores granulares de `mod_calendar_sync_runs` (que el dominio escribe por pull/push y el
    scheduler NO toca). No sumar entre tablas.
    """

    accounts: int = 0
    pulled: int = 0
    consolidated: int = 0
    pushed: int = 0
    errors: int = 0
    steps_failed: list[str] = field(default_factory=list)


def _enabled_accounts(user_id: int) -> list[tuple[int, bool]]:
    """(account_id, write_back) de las cuentas de proveedor habilitadas del user, por id."""
    with connection() as conn:
        rows = conn.execute(
            text(
                "SELECT id, write_back FROM mod_calendar_provider_accounts "
                "WHERE user_id = :uid AND enabled ORDER BY id"
            ),
            {"uid": user_id},
        ).all()
    return [(int(r[0]), bool(r[1])) for r in rows]


async def run_calendar_cycle(user_id: int) -> CalendarCycleStats:
    """Ciclo bidireccional completo de calendar para las cuentas habilitadas del user.

    Orden: pull (por cuenta) → dedup_phase2 → consolidación → merge (user-level, una vez) →
    push (solo cuentas `write_back`). pull/push son por-cuenta; dedup/consolidate/merge son
    user-level. Best-effort por PASO: un paso que falla se loguea, suma a `steps_failed` y no frena
    el resto. `LLMQuotaError` (saldo agotado) corta los pasos LLM restantes pero el push igual se
    intenta (es I/O de proveedor, no LLM).
    """
    cycle = CalendarCycleStats()
    accounts = _enabled_accounts(user_id)
    cycle.accounts = len(accounts)
    quota_exhausted = False

    # 1. pull (ingress) por cuenta
    for account_id, _wb in accounts:
        try:
            pull_stats = await run_pull(user_id, account_id)
            cycle.pulled += pull_stats.pulled
            cycle.errors += pull_stats.errors
        except Exception as e:  # best-effort por cuenta
            cycle.errors += 1
            cycle.steps_failed.append(f"pull:{account_id}")
            _log.warning(
                "scheduler.calendar.step_failed", step="pull", account_id=account_id, error=str(e)
            )

    # 2. dedup FASE 2 (LLM), user-level
    try:
        await run_dedup_phase2(user_id)
    except LLMQuotaError:
        quota_exhausted = True
        cycle.steps_failed.append("dedup:no_quota")
        _log.error("scheduler.calendar.aborted_no_quota", step="dedup")
    except Exception as e:
        cycle.errors += 1
        cycle.steps_failed.append("dedup")
        _log.warning("scheduler.calendar.step_failed", step="dedup", error=str(e))

    # 3. consolidación (determinista, sync), user-level
    try:
        cons_stats = await asyncio.to_thread(run_consolidation, user_id)
        cycle.consolidated += cons_stats.consolidated
    except Exception as e:
        cycle.errors += 1
        cycle.steps_failed.append("consolidate")
        _log.warning("scheduler.calendar.step_failed", step="consolidate", error=str(e))

    # 4. merge (LLM), user-level; se saltea si ya no hay saldo
    if not quota_exhausted:
        try:
            await run_merge(user_id)
        except LLMQuotaError:
            cycle.steps_failed.append("merge:no_quota")
            _log.error("scheduler.calendar.aborted_no_quota", step="merge")
        except Exception as e:
            cycle.errors += 1
            cycle.steps_failed.append("merge")
            _log.warning("scheduler.calendar.step_failed", step="merge", error=str(e))

    # 5. push (egress), solo cuentas write_back; corre aunque falte saldo (no es LLM)
    for account_id, write_back in accounts:
        if not write_back:
            continue
        try:
            push_stats = await run_push(user_id, account_id)
            cycle.pushed += push_stats.created + push_stats.updated + push_stats.deleted
            cycle.errors += push_stats.errors
        except Exception as e:  # best-effort por cuenta
            cycle.errors += 1
            cycle.steps_failed.append(f"push:{account_id}")
            _log.warning(
                "scheduler.calendar.step_failed", step="push", account_id=account_id, error=str(e)
            )

    return cycle


@dataclass
class IdentidadesCycleStats:
    """Roll-up de un ciclo de identidades (sync de contactos por cuenta → desempate LLM)."""

    accounts: int = 0
    synced: int = 0
    merged: int = 0
    linked: int = 0  # pertenencias («sub») seteadas por el organizador LLM
    cooccurrence_edges: int = 0  # aristas confirmed identidad↔identidad del handler de overflow
    errors: int = 0
    steps_failed: list[str] = field(default_factory=list)


def _identidades_accounts(user_id: int) -> list[int]:
    """Ids de las cuentas de proveedor de contactos habilitadas del user."""
    with connection() as conn:
        rows = conn.execute(
            text(
                "SELECT id FROM mod_identidades_provider_accounts "
                "WHERE user_id = :uid AND enabled ORDER BY id"
            ),
            {"uid": user_id},
        ).all()
    return [int(r[0]) for r in rows]


async def run_identidades_cycle(user_id: int) -> IdentidadesCycleStats:
    """Ciclo del módulo identidades: sync (ingress) por cuenta habilitada → desempate LLM de los
    candidatos de merge. Best-effort por paso; `LLMQuotaError` corta el LLM. La construcción del
    grafo (`build_relations`) es on-demand (`POST /graph/build`), no se dispara acá."""
    cycle = IdentidadesCycleStats()
    accounts = _identidades_accounts(user_id)
    cycle.accounts = len(accounts)
    for account_id in accounts:
        try:
            stats = await run_identidades_sync(user_id, account_id)
            cycle.synced += stats.pulled
            cycle.errors += stats.errors
        except Exception as e:  # best-effort por cuenta
            cycle.errors += 1
            cycle.steps_failed.append(f"sync:{account_id}")
            _log.warning(
                "scheduler.identidades.step_failed",
                step="sync",
                account_id=account_id,
                error=str(e),
            )
    try:
        merge_stats = await run_merge_phase2(user_id)
        cycle.merged += merge_stats.merged
        cycle.errors += merge_stats.errors
    except LLMQuotaError:
        cycle.steps_failed.append("merge:no_quota")
        _log.error("scheduler.identidades.aborted_no_quota", step="merge")
    except Exception as e:
        cycle.errors += 1
        cycle.steps_failed.append("merge")
        _log.warning("scheduler.identidades.step_failed", step="merge", error=str(e))
    # Organizar la jerarquía de pertenencia DESPUÉS del dedup (no linkear orgs que se van a fundir).
    try:
        organize_stats = await run_organize(user_id)
        cycle.linked += organize_stats.linked
        cycle.errors += organize_stats.errors
    except LLMQuotaError:
        cycle.steps_failed.append("organize:no_quota")
        _log.error("scheduler.identidades.aborted_no_quota", step="organize")
    except Exception as e:
        cycle.errors += 1
        cycle.steps_failed.append("organize")
        _log.warning("scheduler.identidades.step_failed", step="organize", error=str(e))
    # Co-ocurrencia identidad↔identidad de los correos densos (overflow del tope de fan-out): el
    # LLM decide qué pares se relacionan. ÚLTIMO paso a propósito: corre DESPUÉS del dedup para no
    # emitir aristas a ids que se van a fundir. El tope es configurable (MEMEX_COOCCURRENCE_CAP).
    from memex.config import settings  # import local: estilo del módulo (evita ciclos al importar)

    try:
        cooc_stats = await run_cooccurrence_llm(user_id, cap=settings.cooccurrence_cap)
        cycle.cooccurrence_edges += cooc_stats.edges
        cycle.errors += cooc_stats.errors
    except LLMQuotaError:
        cycle.steps_failed.append("cooccurrence:no_quota")
        _log.error("scheduler.identidades.aborted_no_quota", step="cooccurrence")
    except Exception as e:
        cycle.errors += 1
        cycle.steps_failed.append("cooccurrence")
        _log.warning("scheduler.identidades.step_failed", step="cooccurrence", error=str(e))
    return cycle


# Registry de jobs. NOTA OCR: su claim de `media_assets` NO usa FOR UPDATE SKIP LOCKED → es seguro
# solo porque el scheduler corre los jobs EN SERIE. Si algún día se corren en paralelo, agregar
# SKIP LOCKED al worker de OCR antes de habilitar esa concurrencia.
_REGISTRY: dict[str, Job] = {
    "classify": Job("classify", "PT15M", _sync(run_classification)),
    "summarize": Job("summarize", "PT1H", run_summarization),
    "extract": Job("extract", "PT1H", run_extraction),
    "ocr": Job("ocr", "PT1H", run_ocr),
    "calendar": Job("calendar", "PT30M", run_calendar_cycle),
    "identidades": Job("identidades", "PT1H", run_identidades_cycle),
    "log_purge": Job("log_purge", "P1D", _sync(run_log_purge)),
}


def all_jobs() -> dict[str, Job]:
    """Copia del registry nombre→job (todos los jobs definidos, habilitados o no)."""
    return dict(_REGISTRY)
