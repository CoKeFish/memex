"""Auto-arranque del daemon en Windows vía Tarea Programada (schtasks).

Registra una tarea que corre `daemon start` al iniciar sesión, para que el cliente
ingiera desatendido sin una consola abierta. Solo Windows por ahora (el usuario corre
acá); en otros SO devuelve un mensaje claro en vez de fallar.

La tarea ejecuta el MISMO intérprete que está corriendo (`sys.executable -m
memex_local_client.cli daemon start`), así hereda el venv del cliente. El daemon lee su
config de `~/.memex-local-client/` (paths absolutos), así que no depende del cwd.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

TASK_NAME = "MemexLocalClient"


class AutostartError(Exception):
    """Falló registrar/quitar/consultar la tarea de auto-arranque."""


@dataclass(frozen=True)
class AutostartResult:
    ok: bool
    message: str


def _supported() -> bool:
    return sys.platform == "win32"


def _task_command() -> str:
    """Comando que la tarea ejecuta. `sys.executable` = el python del venv actual."""
    return f'"{sys.executable}" -m memex_local_client.cli daemon start'


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def enable() -> AutostartResult:
    """Crea (o reemplaza) la tarea ONLOGON. Idempotente vía `/F`."""
    if not _supported():
        raise AutostartError(f"auto-arranque solo soportado en Windows (acá: {sys.platform})")
    proc = _run(
        [
            "schtasks",
            "/Create",
            "/TN",
            TASK_NAME,
            "/TR",
            _task_command(),
            "/SC",
            "ONLOGON",
            "/F",
        ]
    )
    if proc.returncode != 0:
        raise AutostartError(
            f"schtasks /Create falló: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return AutostartResult(True, f"tarea {TASK_NAME!r} creada: corre el daemon al iniciar sesión.")


def disable() -> AutostartResult:
    """Quita la tarea. No es error si no existía."""
    if not _supported():
        raise AutostartError(f"auto-arranque solo soportado en Windows (acá: {sys.platform})")
    proc = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    if proc.returncode != 0:
        out = (proc.stderr + proc.stdout).upper()
        if "CANNOT FIND" in out or "DOES NOT EXIST" in out or "NO MAP" in out:
            return AutostartResult(
                True, f"tarea {TASK_NAME!r} no estaba registrada (nada que hacer)."
            )
        raise AutostartError(
            f"schtasks /Delete falló: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return AutostartResult(True, f"tarea {TASK_NAME!r} eliminada.")


def status() -> AutostartResult:
    """True si la tarea está registrada."""
    if not _supported():
        return AutostartResult(False, f"auto-arranque no aplica en {sys.platform} (solo Windows).")
    proc = _run(["schtasks", "/Query", "/TN", TASK_NAME])
    if proc.returncode == 0:
        return AutostartResult(True, f"tarea {TASK_NAME!r} registrada (corre al iniciar sesión).")
    return AutostartResult(
        False, f"tarea {TASK_NAME!r} no registrada. `autostart enable` para activarla."
    )
