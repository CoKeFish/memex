"""Daemon de INGESTA agendada server-side: `memex-ingest-scheduler`.

Hermano de `memex.scheduler` (que agenda PROCESAMIENTO: classify/summarize/extract/…). Acá las
"tareas" son FUENTES: por cada fuente `enabled` con `fetch_schedule` seteado, el daemon dispara un
fetch incremental (`run_fetch_window(mode='incremental', trigger='daemon')`) cada `fetch_schedule`.

Control 100% en runtime desde la DB (igual que el de procesamiento):
- `ingest_scheduler_settings.daemon_enabled` (master, una fila por user) — apagado por default.
- `sources.fetch_schedule` (ISO-8601 por fuente) — NULL = no se agenda.

La fila de cada corrida (start/finalize/fail + stats) la escribe `ingestion_run()` DENTRO de
`run_fetch_window`; este daemon NO toca `worker_runs`.
"""

from memex.ingest_scheduler.daemon import IngestScheduler, ScheduledSource

__all__ = ["IngestScheduler", "ScheduledSource"]
