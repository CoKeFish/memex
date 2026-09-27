"""Nombre visible de un remitente de chat → UNA persona (principio «una cosa del mundo, un
registro»): la organización o el cargo pegados tras un separador son un atributo (pista), no otra
identidad; emojis y tipografías decorativas se limpian; un nombre que es pura decoración se
conserva tal cual (mejor reconocible que mutilado)."""

# ruff: noqa: RUF001  (los casos SON tipografías decorativas y emojis: eso es lo que se prueba)
from __future__ import annotations

import pytest

from memex.modules.identidades.normalize import clean_display_name, split_display_name


@pytest.mark.parametrize(
    ("raw", "name", "org"),
    [
        ("Pablo Vera | GrantBox", "Pablo Vera", "GrantBox"),
        ("Marta - CoopBlocks", "Marta", "CoopBlocks"),
        ("Don Beto | Tienda.so", "Don Beto", "Tienda.so"),
        ("Emanuel Aguirre | Cavox", "Emanuel Aguirre", "Cavox"),
        ("Ana María (Diseño)", "Ana María", "Diseño"),
        ("Lau @ Acme", "Lau", "Acme"),
        ("Marty 🤪", "Marty", ""),
        ("Lucía Reyes 🐲", "Lucía Reyes", ""),
        ("𝔇𝔢𝔩𝔰𝔦𝔫 𝚁𝚘𝚠𝚎🍓", "Delsin Rowe", ""),
        ("𝐽𝑢𝑙𝑖𝑎𝑛𝑎", "Juliana", ""),
        ("M And L", "M And L", ""),
        ("Querida Eugenia", "Querida Eugenia", ""),
        ("Jean-Paul Sartre", "Jean-Paul Sartre", ""),  # guion sin espacios: nombre compuesto
        ("L.", "L.", ""),  # una sola letra: se conserva
    ],
)
def test_split_display_name(raw: str, name: str, org: str) -> None:
    assert split_display_name(raw) == (name, org)


def test_pura_decoracion_se_conserva() -> None:
    raw = "༒•∂ᖇ◎𝕎𝓧ᵉŞ•༒"
    assert clean_display_name(raw) == raw
    assert split_display_name(raw) == (raw, "")
    assert split_display_name("🦄✨⚡🔥💯") == ("🦄✨⚡🔥💯", "")


def test_no_parte_si_el_lado_izquierdo_no_parece_nombre() -> None:
    # dígitos o demasiadas palabras a la izquierda: no es «nombre + org», queda entero y limpio
    assert split_display_name("Promo 2024 | Tienda") == ("Promo 2024 Tienda", "")
    largo = "Uno Dos Tres Cuatro Cinco Seis | Org"
    assert split_display_name(largo) == ("Uno Dos Tres Cuatro Cinco Seis Org", "")


def test_espacios_y_vacio() -> None:
    assert split_display_name("  Ana   López  ") == ("Ana López", "")
    assert split_display_name("") == ("", "")
