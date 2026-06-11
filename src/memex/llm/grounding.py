"""Grounder compartido: verificación DETERMINISTA de que una cita del LLM está en la evidencia.

Patrón ODKE+ (el «grounder» que exige soporte textual explícito antes de aceptar un hecho): el LLM
debe citar el fragmento que justifica su veredicto y la contención se verifica acá, sin red — un
veredicto sin cita verificable se descarta/degrada en el caller. Extraído de
`modules/identidades/relations_llm.py` (vía >8) para reusarlo en el resolver par-por-par del grafo;
el comportamiento es idéntico al original.
"""

from __future__ import annotations

#: Largo mínimo del quote NORMALIZADO: mata citas trivialmente contenidas ("y", "de", un nombre
#: suelto) sin exigir frases largas. Es la perilla de calibración del grounder: si `ungrounded`
#: sale alto en corridas reales, se revisa ANTES de relajar la normalización.
DEFAULT_MIN_QUOTE_NORM_LEN = 10


def norm_grounding(s: str) -> str:
    """Normalización del check de contención: lower + colapso de TODO whitespace. Deliberadamente
    SIN unaccent ni strip de puntuación — la estrictez es el sesgo a precisión; calibrar con
    `ungrounded` antes de relajar."""
    return " ".join(s.lower().split())


def grounded(quote: str, *evidences: str, min_len: int = DEFAULT_MIN_QUOTE_NORM_LEN) -> bool:
    """¿La cita está realmente en ALGUNA de las evidencias que el LLM vio? Determinista: largo
    mínimo normalizado + substring sobre las MISMAS strings (truncadas igual) del prompt."""
    q = norm_grounding(quote)
    if len(q) < min_len:
        return False
    return any(q in norm_grounding(ev) for ev in evidences)
