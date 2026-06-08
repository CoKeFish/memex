"""Sistema de calidad / relevancia: cuantifica qué tan relevante es cada remitente/canal.

Núcleo determinista (SQL puro, cero LLM): la señal de relevancia por mensaje sale de
`module_extractions.item_count` (¿produjo un hecho de dominio más allá de identidad?),
co-condicionada con el summarizer (valor de lectura). Se agrega por remitente para domar ruido.
"""
