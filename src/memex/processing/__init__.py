"""Substrato de procesamiento compartido entre relations/summary.py y los módulos de extracción.

El *windowing* (agrupar el work-set clasificado en ventanas batch / individual) es idéntico
para ambos: el resumen y la extracción operan sobre los MISMOS mensajes clasificados originales
(ADR-015 §9, "etapa combinada"). Vive acá para que no diverja.
"""
