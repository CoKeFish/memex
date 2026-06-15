from __future__ import annotations

import subprocess

import pytest

from memex_local_client import autostart


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_enable_creates_onlogon_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "_supported", lambda: True)
    calls: dict[str, list[str]] = {}

    def fake_run(args: list[str]) -> _FakeProc:
        calls["args"] = args
        return _FakeProc(0)

    monkeypatch.setattr(autostart, "_run", fake_run)
    res = autostart.enable()
    assert res.ok
    assert "/Create" in calls["args"]
    assert "ONLOGON" in calls["args"]
    assert autostart.TASK_NAME in calls["args"]


def test_enable_unsupported_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "_supported", lambda: False)
    with pytest.raises(autostart.AutostartError):
        autostart.enable()


def test_disable_missing_task_is_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "_supported", lambda: True)
    monkeypatch.setattr(
        autostart,
        "_run",
        lambda args: _FakeProc(1, stderr="ERROR: The system cannot find the file specified."),
    )
    res = autostart.disable()
    assert res.ok
    assert "no estaba registrada" in res.message


def test_disable_real_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "_supported", lambda: True)
    monkeypatch.setattr(
        autostart, "_run", lambda args: _FakeProc(1, stderr="ERROR: Access denied.")
    )
    with pytest.raises(autostart.AutostartError):
        autostart.disable()


def test_status_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "_supported", lambda: True)
    monkeypatch.setattr(autostart, "_run", lambda args: _FakeProc(0))
    res = autostart.status()
    assert res.ok
    assert autostart.TASK_NAME in res.message


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
