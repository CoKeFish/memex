"""Ingestor de Notion: apuntes PROPIOS del usuario como fuente `notes` (tipo `notion`).

Solo las páginas raíz configuradas (`roots`) y sus subpáginas; incremental por `last_edited_time`
(una página editada vuelve a entrar y REEMPLAZA su registro). Sin DB ni API de memex acá (ADR-001
y `tests/test_typing_discipline.py`): habla con la API de Notion y devuelve `SourceRecord`s.
"""
