"""CLI `memex-notifications`: prender/apagar, productores, canales, un ciclo en dry-run."""

from __future__ import annotations

import pytest

from memex.db import connection
from memex.notifications import store
from memex.notifications.cli import main
from memex.notifications.settings import get_settings


def test_enable_disable_and_status(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["enable"]) == 0
    with connection() as c:
        assert get_settings(c, 1).daemon_enabled is True
    assert main(["disable"]) == 0
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "daemon: apagado" in out
    assert "resumen_diario" in out and "recordatorio_evento" in out and "(defaults)" in out


def test_producers_set_validates_and_persists(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["producers", "set", "resumen_diario", "--set", "hora=06:30", "--render", "plantilla"])
        == 0
    )
    assert "hora=06:30" in capsys.readouterr().out
    assert main(["producers", "set", "resumen_diario", "--set", "hora=mañana"]) == 2
    assert main(["producers", "set", "recordatorio_evento", "--render", "llm"]) == 2
    assert main(["producers", "set", "inexistente", "--enable"]) == 2
    assert main(["producers", "set", "tareas_del_dia", "--disable"]) == 0
    assert main(["producers", "show"]) == 0
    out = capsys.readouterr().out
    assert "tareas_del_dia       apagado" in out


def test_channels_add_list_toggle_rm(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["channels", "add", "telegram", "--target", "abc"]) == 2
    assert (
        main(
            [
                "channels",
                "add",
                "telegram",
                "--target",
                "7289",
                "--sistema",
                "alta",
                "--quiet",
                "22:00-07:00",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "telegram → 7289" in out and "sistema≥alta" in out and "silencio 22:00-07:00" in out
    with connection() as c:
        cid = store.list_channels(c, user_id=1)[0]["id"]
    assert main(["channels", "disable", str(cid)]) == 0
    assert main(["channels", "list"]) == 0
    assert "apagado" in capsys.readouterr().out
    assert main(["channels", "rm", str(cid)]) == 0
    assert main(["channels", "rm", str(cid)]) == 1


def test_run_dry_run_reports_without_writing(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--dry-run", "--producer", "sistema"]) == 0
    assert "productores=['sistema']" in capsys.readouterr().out
    with connection() as c:
        assert store.list_active(c, user_id=1, limit=5) == []
    assert main(["run", "--producer", "nadie"]) == 2
    assert main(["deliveries"]) == 0
    assert "(sin entregas)" in capsys.readouterr().out
