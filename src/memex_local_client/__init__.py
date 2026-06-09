"""memex_local_client — cliente local de ingestión (corre en el PC del usuario).

Cumple el rol de Ingestor en ADR-001: conoce las fuentes externas, mantiene
credenciales locales, aplica filtro pre-ingest, y postea registros al gateway
de memex en VPS. Nunca importa internals del servidor (memex.api, memex.db).
Se permite importar `memex.core.*`, `memex.ingestors.*` y `memex.logging`.

Arquitectura interna:

- `protocol`  — el contrato `LocalPlugin` que cada fuente concreta cumple.
- `discovery` — carga dinámica de plugins desde `~/.memex-local-client/plugins/`.
- `registry`  — qué plugins están habilitados (persistido en SQLite local).
- `state`     — SQLite local con plugins y runs (los cursores y el dedup viven server-side).
- `scheduler` — loop principal del daemon, agenda y dispara plugins.
- `run`       — wrapper sobre `memex.ingestors.runner.run_ingestor` con
                bookkeeping local.
- `cli`       — entry point `memex-local-client`: daemon start/stop, plugin
                install/enable/disable/doctor/authorize, status, runs.
- `config`    — paths bajo `~/.memex-local-client/` y carga del config principal
                (URL del gateway, token).

Los plugins NO viven dentro de este paquete (excepto los bundled de muestra).
Cada plugin es un módulo independiente que el daemon descubre por filesystem.
"""
