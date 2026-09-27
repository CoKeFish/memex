"""Capa de grafo: vértices (filas `mod_*` únicas + vértices nativos del grafo) y aristas.

Un solo grafo con todos los datos del usuario. Un VÉRTICE = una entidad limpia y única producida por
un módulo (gasto, evento consolidado, persona/organización, hackatón). Una ARISTA = una referencia
tipada entre dos vértices, que SIEMPRE registra su PRODUCTOR (quién la formó: inbox/dedup/
consolidación/llm/humano). CUALQUIER vértice puede conectarse con cualquiera (sin ontología).

Este paquete se arma por fases: `edges` (almacén/repositorio de aristas) primero; la proyección de
vértices, el paso determinista de relaciones, el pre-filtro de candidatos y el decisor LLM
(+ cúmulos como vértices nativos) llegan después. Aislamiento (ADR-001): recibe la `Connection`
inyectada; no importa db/llm directo.
"""
