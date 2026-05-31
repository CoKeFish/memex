"""Daemon server-side que corre los workers idempotentes (classify/summarize/extract/calendar)
en intervalos. Hermano server-side del daemon de plugins de `memex_local_client`.

Apagado por default: arranca DESARMADO (`SchedulerSettings.enabled_jobs` vacío) → idlea sin
procesar nada hasta que el dueño lo arme explícitamente. El backlog se procesa de forma manual y
vigilada antes (CLIs por-worker o `memex-scheduler run <job> --limit`).
"""
