"""Categorías y normalización del módulo bienestar (registrador determinista, sin LLM)."""

from __future__ import annotations

#: Buckets gruesos válidos (lista cerrada). 'otros' = catch-all para lo que no encaja en ninguno.
BIENESTAR_CATEGORIES: tuple[str, ...] = (
    "comida",
    "higiene",
    "ejercicio",
    "grooming",
    "salud",
    "otros",
)
_CATEGORY_SET = frozenset(BIENESTAR_CATEGORIES)


def normalize_category(value: str) -> str:
    """Categoría a minúsculas; fuera de la lista cerrada → 'otros' (no se rechaza el registro)."""
    s = (value or "").strip().lower()
    return s if s in _CATEGORY_SET else "otros"


def normalize_activity(value: str) -> str:
    """Colapsa whitespace. La actividad es la clave de adherencia futura; el match real lo hace
    la DB normalizado (insensible a mayúsculas/espacios); esto solo limpia el valor guardado."""
    return " ".join((value or "").split())
