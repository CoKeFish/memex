"""Auto-arranque del daemon en Windows: Tarea Programada, con respaldo en la carpeta de Inicio.

El cliente local debe ingerir desatendido, sin una consola abierta ni una sesión viva. Dos
mecanismos, en orden de preferencia:

1. **Tarea Programada** (`schtasks /SC ONLOGON`): lo mejor (la gestiona el Programador de tareas,
   sobrevive a cambios de perfil), pero registrar un disparador ONLOGON **exige permisos de
   administrador**. Sin elevación devuelve "Access is denied".
2. **Carpeta de Inicio** (`shell:startup`): un `.cmd` que Windows ejecuta al iniciar sesión. NO
   necesita administrador, que es el caso normal en una máquina personal. Se lanza con
   `pythonw.exe` (el intérprete sin consola) para que no aparezca una ventana negra.

`enable()` intenta 1 y, si falta elevación, cae a 2 informando cuál quedó. `status()` reporta
cualquiera de los dos y `disable()` quita ambos, así no quedan dos arranques compitiendo.

Ambos mecanismos ejecutan el MISMO intérprete que está corriendo (el venv del cliente). El daemon
lee su config de `~/.memex-local-client/` (paths absolutos), así que no depende del cwd.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TASK_NAME = "MemexLocalClient"
#: Nombre del .cmd en la carpeta de Inicio (mecanismo de respaldo, sin admin).
STARTUP_ENTRY = "memex-local-client.cmd"


class AutostartError(Exception):
    """Falló registrar/quitar/consultar el auto-arranque."""


@dataclass(frozen=True)
class AutostartResult:
    ok: bool
    message: str
    #: Qué mecanismo quedó activo: "task" (Tarea Programada), "startup" (carpeta de Inicio) o "".
    mechanism: str = ""


def _supported() -> bool:
    return sys.platform == "win32"


def _pythonw() -> str:
    """`pythonw.exe` del mismo venv (python sin consola). Si no está, `python.exe`."""
    exe = Path(sys.executable)
    noconsole = exe.with_name("pythonw.exe")
    return str(noconsole if noconsole.exists() else exe)


def _task_command() -> str:
    """Comando que la tarea ejecuta. `sys.executable` = el python del venv actual."""
    return f'"{sys.executable}" -m memex_local_client.cli daemon start'


def startup_dir() -> Path:
    """Carpeta de Inicio del usuario (lo que abre `shell:startup`)."""
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _startup_file() -> Path:
    return startup_dir() / STARTUP_ENTRY


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _is_access_denied(proc: subprocess.CompletedProcess[str]) -> bool:
    out = (proc.stderr + proc.stdout).upper()
    return "ACCESS IS DENIED" in out or "ACCESO DENEGADO" in out


def _enable_task() -> subprocess.CompletedProcess[str]:
    return _run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", _task_command(), "/SC", "ONLOGON", "/F"]
    )


def _enable_startup() -> Path:
    """Escribe el .cmd en la carpeta de Inicio. Idempotente (sobrescribe)."""
    path = _startup_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    # `start ""` desacopla el proceso para que la ventana de cmd no quede colgada esperándolo.
    path.write_text(
        "@echo off\r\n"
        "rem Auto-arranque del cliente local de memex (generado por `memex-local-client "
        "autostart enable`).\r\n"
        f'start "" "{_pythonw()}" -m memex_local_client.cli daemon start\r\n',
        encoding="utf-8",
    )
    return path


def enable() -> AutostartResult:
    """Registra el auto-arranque. Tarea Programada si hay permisos; si no, carpeta de Inicio."""
    if not _supported():
        raise AutostartError(f"auto-arranque solo soportado en Windows (acá: {sys.platform})")
    proc = _enable_task()
    if proc.returncode == 0:
        _remove_startup()  # no dejar los dos compitiendo
        return AutostartResult(
            True, f"tarea {TASK_NAME!r} creada: corre el daemon al iniciar sesión.", "task"
        )
    if not _is_access_denied(proc):
        raise AutostartError(
            f"schtasks /Create falló: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    path = _enable_startup()
    return AutostartResult(
        True,
        f"sin permisos para una Tarea Programada (necesita admin); registrado en la carpeta de "
        f"Inicio: {path}. Corre el daemon al iniciar sesión, sin ventana.",
        "startup",
    )


def _remove_startup() -> bool:
    path = _startup_file()
    if path.exists():
        path.unlink()
        return True
    return False


def disable() -> AutostartResult:
    """Quita AMBOS mecanismos. No es error si no estaban."""
    if not _supported():
        raise AutostartError(f"auto-arranque solo soportado en Windows (acá: {sys.platform})")
    removed: list[str] = []
    proc = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    if proc.returncode == 0:
        removed.append("tarea programada")
    else:
        out = (proc.stderr + proc.stdout).upper()
        existed = not ("CANNOT FIND" in out or "DOES NOT EXIST" in out or "NO MAP" in out)
        if existed and not _is_access_denied(proc):
            raise AutostartError(
                f"schtasks /Delete falló: {proc.stderr.strip() or proc.stdout.strip()}"
            )
    if _remove_startup():
        removed.append("carpeta de Inicio")
    if not removed:
        return AutostartResult(True, "el auto-arranque no estaba registrado (nada que hacer).")
    return AutostartResult(True, f"auto-arranque eliminado ({', '.join(removed)}).")


def status() -> AutostartResult:
    """Reporta si hay auto-arranque y por cuál mecanismo."""
    if not _supported():
        return AutostartResult(False, f"auto-arranque no aplica en {sys.platform} (solo Windows).")
    if _run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0:
        return AutostartResult(
            True, f"tarea {TASK_NAME!r} registrada (corre al iniciar sesión).", "task"
        )
    path = _startup_file()
    if path.exists():
        return AutostartResult(True, f"registrado en la carpeta de Inicio: {path}", "startup")
    return AutostartResult(
        False,
        f"auto-arranque no registrado. `autostart enable` para activarlo (sin admin usa la "
        f"carpeta de Inicio: {startup_dir()}).",
    )
