"""Guarda de datos personales: ningún commit puede llevar identificadores del dueño ni de terceros.

El repo se comparte; los prompts, tests y docs describen categorías, no casos reales (regla
«prompts sin marcas», ampliada a todo el árbol el 2026-09-26 tras la auditoría). La lista de
identificadores vive FUERA del repo (`memex.privacy`: `$MEMEX_PERSONAL_DENYLIST` o
`~/.memex/personal-denylist.txt`).

Uso:
  python scripts/check_personal_data.py ARCHIVO...   # lo que pre-commit pasa (staged)
  python scripts/check_personal_data.py --all        # todo el árbol versionado

Sin lista (otra máquina, CI) avisa y deja pasar: la guarda es del dueño, no del proyecto. Con
hallazgos imprime `archivo:línea: término` y sale 1.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from memex.privacy import denylist_path, find_hits, load_denylist

SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".woff",
    ".woff2",
    ".ttf",
    ".lock",
}
SKIP_NAMES = {"package-lock.json"}


def tracked_files() -> list[str]:
    res = subprocess.run(["git", "ls-files"], check=True, capture_output=True, text=True)
    return [line for line in res.stdout.splitlines() if line]


def main(argv: list[str]) -> int:
    rules = load_denylist()
    if not rules:
        print(
            f"check_personal_data: sin lista en {denylist_path()}; nada que comprobar",
            file=sys.stderr,
        )
        return 0
    paths = tracked_files() if "--all" in argv else [a for a in argv if not a.startswith("--")]
    hits: list[str] = []
    for p in paths:
        path = Path(p)
        if not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES or path.name in SKIP_NAMES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits.extend(f"{p}:{n}: {term}" for n, term in find_hits(text, rules))
    if not hits:
        return 0
    print("Datos personales en el árbol (lista externa):", file=sys.stderr)
    for h in hits:
        print("  " + h, file=sys.stderr)
    print(
        f"{len(hits)} hallazgo(s). Reemplazá por datos ficticios antes de commitear.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
