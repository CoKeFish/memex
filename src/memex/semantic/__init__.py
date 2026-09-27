"""Bloqueo SEMÁNTICO compartido: embeddings de textos cortos + vecinos por coseno.

Por qué (2026-09-24): toda la detección de «misma cosa» de memex era léxica (tokens compartidos,
difflib, trigramas) y hacía de compuerta antes del juez LLM. Dos registros de la misma cosa con
otras palabras («reclamar el pasaporte» / «recoger y revisar el pasaporte») no compartían ningún
token útil y el juez nunca los veía. Es el problema clásico de *entity matching*: la literatura
(DeepBlocker, Ditto, Peeters & Bizer 2023) separa el BLOQUEO (barato, con alto recall: aquí
embeddings + coseno) del MATCHING (preciso: aquí el juez LLM que cada módulo ya tiene).

Qué hay:
- `embedder`: quién calcula vectores. Por default un modelo LOCAL multilingüe (fastembed/ONNX, sin
  red, sin costo, determinista); un doble fijo para tests. Se elige por settings.
- `store`: la tabla `semantic_vectors` (un vector por registro y modelo) y `neighbors`: los vecinos
  por coseno dentro de un ALCANCE chico (los ids vivos de un asunto, las completadas de un
  asunto…). El cálculo es en Python sobre decenas o cientos de filas: no hace falta pgvector.

Qué NO hace: nunca decide solo. Propone candidatos; el juez de cada módulo (tareas_judge,
calendar_dedup, identidades_dedup…) decide. Medido sobre datos reales del dueño (2026-09-24, 71
tareas vivas): los duplicados que la compuerta léxica dejó pasar puntúan entre 0,85 y 0,89 con el
modelo chico y el percentil 95 de los pares normales del mismo asunto es 0,55.
"""
