"""Classifier post-ingest: asigna un tier (ADR-002) a cada mensaje del inbox.

Worker server-side y determinístico (sin LLM). `rules.classify` decide el tier a
partir del payload; `worker.run_classification` lo aplica sobre el inbox no-clasificado
y llena la tabla `classifications` (migración 0005). La CLI `memex-classify` lo dispara.
"""
