"""Módulo `bienestar` (salud y bienestar): registrador DETERMINISTA de eventos auto-reportados.

NO es un módulo de extracción: no usa LLM ni consume mensajes ingeridos. Un agente externo (p. ej.
Hermes) entiende el lenguaje natural por Telegram y llama a la CLI `memex-bienestar` con campos ya
estructurados. Ver `memex.modules.bienestar.module` (register / list_registros / summary).
"""
