"""Backfill segmentado: importa un rango histórico en ventanas con frontera persistida.

El usuario fija `[fecha1, fecha2]` para una fuente de correo y avanza la importación en ventanas
(día/semana/mes x N) apretando un botón; la frontera se guarda en `backfill_jobs`, así recargar el
navegador no pierde el avance. Cada ventana reusa el camino `mode=range` del fetch a demanda
(`memex.api.fetch_runner.run_fetch_window`): inserta sin tocar el cursor incremental y el dedup
`UNIQUE(source_id, external_id)` hace idempotente re-correr una ventana interrumpida.
"""
