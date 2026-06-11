"""Grounder compartido (`memex.llm.grounding`): paridad con el comportamiento que tenía en
identidades — normalización lower+whitespace, largo mínimo, contención multi-evidencia.
"""

from __future__ import annotations

from memex.llm.grounding import grounded, norm_grounding


def test_norm_lower_y_colapso_de_whitespace() -> None:
    assert norm_grounding("  Juan\n  trabaja\tcon  ACME ") == "juan trabaja con acme"
    # SIN unaccent ni strip de puntuación (sesgo a precisión deliberado)
    assert norm_grounding("Joaquín, S.A.") == "joaquín, s.a."


def test_grounded_substring_en_cualquier_evidencia() -> None:
    assert grounded("trabaja con Acme", "no está acá", "Juan TRABAJA   con acme desde 2020")
    assert grounded("trabaja con Acme", "juan trabaja con acme") is True
    assert grounded("trabaja con Acme", "otra cosa totalmente") is False


def test_grounded_largo_minimo() -> None:
    # corto normalizado (< 10) se descarta aunque esté contenido
    assert grounded("y", "x y z son letras") is False
    assert grounded("de", "viene de lejos por acá") is False
    # con min_len explícito más chico, pasa
    assert grounded("y", "x y z", min_len=1) is True


def test_grounded_sin_evidencias() -> None:
    assert grounded("una cita suficientemente larga") is False
