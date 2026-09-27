"""Lista externa de identificadores personales del dueño (nombres, correos, teléfonos, ids de
chat, organizaciones de su vida) que no deben aparecer en el código, los tests, los docs ni los
prompts.

La lista vive FUERA del repo (`$MEMEX_PERSONAL_DENYLIST` o `~/.memex/personal-denylist.txt`):
si estuviera adentro sería justo lo que no queremos publicar. Formato: un término por línea, `#`
comenta, `re:` al inicio = expresión regular; el resto se compara por palabra completa, sin
distinguir mayúsculas. Sin archivo, la lista es vacía (otra máquina, CI): la guarda es del dueño.

La usan `scripts/check_personal_data.py` (pre-commit, sobre los archivos del commit) y
`tests/test_principios.py` (los prompts no nombran nada de la lista). Auditoría 2026-09-26."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DENYLIST = Path.home() / ".memex" / "personal-denylist.txt"


@dataclass(frozen=True)
class DenyRule:
    term: str
    pattern: re.Pattern[str]

    def hits(self, text: str) -> bool:
        return self.pattern.search(text) is not None


def denylist_path() -> Path:
    return Path(os.environ.get("MEMEX_PERSONAL_DENYLIST") or DEFAULT_DENYLIST)


def parse_denylist(text: str) -> list[DenyRule]:
    out: list[DenyRule] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            out.append(DenyRule(line, re.compile(line[3:], re.IGNORECASE)))
        else:
            out.append(
                DenyRule(line, re.compile(rf"(?<![\w])({re.escape(line)})(?![\w])", re.IGNORECASE))
            )
    return out


def load_denylist(path: Path | None = None) -> list[DenyRule]:
    p = path or denylist_path()
    if not p.is_file():
        return []
    return parse_denylist(p.read_text(encoding="utf-8"))


def find_hits(text: str, rules: list[DenyRule]) -> list[tuple[int, str]]:
    """(línea, término) por cada aparición en `text`."""
    out: list[tuple[int, str]] = []
    for n, line in enumerate(text.splitlines(), 1):
        for r in rules:
            if r.hits(line):
                out.append((n, r.term))
    return out
