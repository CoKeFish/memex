"""Subsistema de transporte — el consumidor reactivo de "¿llego a tiempo?".

Cruza el próximo evento (calendar) + tu ubicación actual (geo/pings) + el tiempo de viaje
(geo/maps) y emite el aviso contra el seam `Notifier`. Reactivo y determinista: vive fuera de `geo`
(librería pura) y de `calendar` (lotes post-mensajes) porque importa a ambos y corre por tiempo, no
por la llegada de un mensaje. Importá los submódulos directamente
(`memex.transport.reachability` / `.config` / `.service` / `.job`).
"""

from __future__ import annotations
