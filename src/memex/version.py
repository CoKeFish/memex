"""Versión del CÓDIGO que corre, para sellar QUÉ versión ingirió, clasificó, juzgó y extrajo cada
mensaje (columnas `code_version`) y exponerla en `GET /version`.

Por qué: los datos de dev se borran y se regeneran, pero mientras tanto conviven filas producidas
por parsers/prompts distintos (cuerpos con relleno invisible, horas en UTC…). Sin sello no hay forma
de saber si un resultado raro es del código actual o de uno anterior.

Resolución, en orden:
1. `MEMEX_CODE_VERSION` en el entorno — la imagen Docker la recibe como build-arg
   (`scripts/build_api.ps1` la calcula con git; `docker compose build` a secas deja «unknown»).
2. `git rev-parse` sobre el repo que contiene este paquete — host: CLI, daemon, tests — con sufijo
   `-dirty` si hay cambios sin commitear.
3. `"unknown"`: visible en la UI y en /version para que no pase desapercibido.
"""

from __future__ import annotations

import functools
import os
import pathlib
import subprocess

ENV_VAR = "MEMEX_CODE_VERSION"
UNKNOWN = "unknown"
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]  # src/memex/version.py → raíz del repo


@functools.lru_cache(maxsize=1)
def code_version() -> str:
    """Identificador corto y estable del código en ejecución (cacheado por proceso)."""
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return env
    return _git_version() or UNKNOWN


def _git_version() -> str | None:
    """`<sha corto>[-dirty]` del repo, o None si no hay repo/git (p.ej. dentro de la imagen)."""
    if not (_REPO_ROOT / ".git").exists():  # en un worktree `.git` es un archivo: también vale
        return None
    try:
        sha = _git("rev-parse", "--short=9", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return None
    if not sha:
        return None
    try:
        dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    except (OSError, subprocess.SubprocessError):
        dirty = False
    return f"{sha}-dirty" if dirty else sha


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(_REPO_ROOT), *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.strip()
