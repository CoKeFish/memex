"""Backfill: rellenar huecos históricos de datos ya ingeridos. Dos piezas, ambas server-side
(pueden importar `memex.api`/`memex.db`, a diferencia de `ingestors/`):

  * backfill SEGMENTADO de correo (`service.py`/`windows.py`): trae un rango de fechas que nunca
    se ingirió, en ventanas con frontera persistida. Descrito abajo.
  * backfill de ADJUNTOS (`backfill.media`): re-baja por IMAP los bytes de adjuntos de correos
    YA ingeridos que no tienen `media_asset`, sin tocar el inbox.

---

Backfill segmentado: importa un rango histórico en ventanas con frontera persistida.

El usuario fija `[fecha1, fecha2]` para una fuente de correo y avanza la importación en ventanas
(día/semana/mes x N) apretando un botón; la frontera se guarda en `backfill_jobs`, así recargar el
navegador no pierde el avance. Cada ventana reusa el camino `mode=range` del fetch a demanda
(`memex.api.fetch_runner.run_fetch_window`): inserta sin tocar el cursor incremental y el dedup
`UNIQUE(source_id, external_id)` hace idempotente re-correr una ventana interrumpida.
"""
