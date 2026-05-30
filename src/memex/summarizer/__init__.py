"""Summarizer: primera mitad de la etapa combinada (ADR-015, corregido).

Lee mensajes CLASIFICADOS originales (no el resumen — cero pérdida) y produce el
resumen (tu memoria) en `summaries` + `summary_inbox_links`. La segunda mitad —la
extracción de módulos sobre los MISMOS originales— vivirá en este paquete más adelante.

Worker server-side (usa memex.db directo) y async (la capa LLM es async). Saltea el tier
`blacklist`; resume `batch` por ventanas y `individual` 1:1.
"""
