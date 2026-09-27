"""Auto-arranque del cliente local: Tarea Programada y el respaldo sin admin (carpeta de Inicio).

`schtasks /SC ONLOGON` exige elevación; en una máquina personal normalmente NO la hay y devuelve
"Access is denied". Ahí el auto-arranque cae a un `.cmd` en la carpeta de Inicio, que no necesita
permisos. Los tests aíslan esa carpeta en `tmp_path` (nunca tocan el APPDATA real).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from memex_local_client import autostart

ACCESS_DENIED = "ERROR: Access is denied."


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def _isolated_startup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """La carpeta de Inicio del test: nunca la real del usuario."""
    startup = tmp_path / "Startup"
    startup.mkdir()
    monkeypatch.setattr(autostart, "startup_dir", lambda: startup)
    return startup


def test_enable_creates_onlogon_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "_supported", lambda: True)
    calls: dict[str, list[str]] = {}

    def fake_run(args: list[str]) -> _FakeProc:
        calls["args"] = args
        return _FakeProc(0)

    monkeypatch.setattr(autostart, "_run", fake_run)
    res = autostart.enable()
    assert res.ok and res.mechanism == "task"
    assert "/Create" in calls["args"]
    assert "ONLOGON" in calls["args"]
    assert autostart.TASK_NAME in calls["args"]


def test_enable_falls_back_to_startup_folder_without_admin(
    monkeypatch: pytest.MonkeyPatch, _isolated_startup: Path
) -> None:
    """Sin permisos de admin, el auto-arranque igual queda: un .cmd en la carpeta de Inicio que
    lanza el daemon con pythonw (sin ventana)."""
    monkeypatch.setattr(autostart, "_supported", lambda: True)
    monkeypatch.setattr(autostart, "_run", lambda args: _FakeProc(1, stderr=ACCESS_DENIED))

    res = autostart.enable()

    assert res.ok and res.mechanism == "startup"
    entry = _isolated_startup / autostart.STARTUP_ENTRY
    assert entry.exists()
    body = entry.read_text(encoding="utf-8")
    assert "memex_local_client.cli daemon start" in body
    assert body.startswith("@echo off")


def test_enable_by_task_removes_a_stale_startup_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Si antes hubo respaldo y ahora sí hay permisos, no quedan DOS arranques compitiendo."""
    monkeypatch.setattr(autostart, "_supported", lambda: True)
    monkeypatch.setattr(autostart, "_run", lambda args: _FakeProc(1, stderr=ACCESS_DENIED))
    autostart.enable()
    assert autostart.status().mechanism == "startup"

    monkeypatch.setattr(autostart, "_run", lambda args: _FakeProc(0))
    res = autostart.enable()

    assert res.mechanism == "task"
    assert not (autostart.startup_dir() / autostart.STARTUP_ENTRY).exists()


def test_enable_other_schtasks_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un fallo que NO es de permisos no se disfraza con el respaldo: se reporta."""
    monkeypatch.setattr(autostart, "_supported", lambda: True)
    monkeypatch.setattr(autostart, "_run", lambda args: _FakeProc(1, stderr="ERROR: bad syntax"))
    with pytest.raises(autostart.AutostartError):
        autostart.enable()


def test_enable_unsupported_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "_supported", lambda: False)
    with pytest.raises(autostart.AutostartError):
        autostart.enable()


def test_disable_missing_autostart_is_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "_supported", lambda: True)
    monkeypatch.setattr(
        autostart,
        "_run",
        lambda args: _FakeProc(1, stderr="ERROR: The system cannot find the file specified."),
    )
    res = autostart.disable()
    assert res.ok
    assert "no estaba registrado" in res.message


def test_disable_removes_the_startup_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "_supported", lambda: True)
    monkeypatch.setattr(autostart, "_run", lambda args: _FakeProc(1, stderr=ACCESS_DENIED))
    autostart.enable()

    monkeypatch.setattr(
        autostart, "_run", lambda args: _FakeProc(1, stderr="ERROR: cannot find the file")
    )
    res = autostart.disable()

    assert res.ok and "carpeta de Inicio" in res.message
    assert not (autostart.startup_dir() / autostart.STARTUP_ENTRY).exists()
    assert autostart.status().ok is False


def test_disable_real_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "_supported", lambda: True)
    monkeypatch.setattr(
        autostart, "_run", lambda args: _FakeProc(1, stderr="ERROR: something exploded.")
    )
    with pytest.raises(autostart.AutostartError):
        autostart.disable()


def test_status_registered_as_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "_supported", lambda: True)
    monkeypatch.setattr(autostart, "_run", lambda args: _FakeProc(0))
    res = autostart.status()
    assert res.ok and res.mechanism == "task"
    assert autostart.TASK_NAME in res.message


def test_status_registered_in_startup_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "_supported", lambda: True)
    monkeypatch.setattr(autostart, "_run", lambda args: _FakeProc(1))  # no hay tarea
    (autostart.startup_dir() / autostart.STARTUP_ENTRY).write_text("@echo off", encoding="utf-8")
    res = autostart.status()
    assert res.ok and res.mechanism == "startup"
    assert autostart.STARTUP_ENTRY in res.message


def test_status_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "_supported", lambda: False)
    res = autostart.status()
    assert res.ok is False
    assert "Windows" in res.message


def test_run_uses_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    # _run delega a subprocess.run sin shell, capturando salida.
    seen: dict[str, object] = {}

    def fake_subprocess_run(args: list[str], **kwargs: object) -> _FakeProc:
        seen["args"] = args
        seen["kwargs"] = kwargs
        return _FakeProc(0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    autostart._run(["schtasks", "/Query"])
    assert seen["args"] == ["schtasks", "/Query"]
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("check") is False
