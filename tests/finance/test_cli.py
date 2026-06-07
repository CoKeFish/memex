"""CLI `memex-finance`: el subcomando `help` lista los comandos (descubrimiento para el agente)."""

from __future__ import annotations

import pytest

from memex.modules.finance.cli import main


def test_cli_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "memex-finance" in out
    assert "register" in out
    assert "--event" in out
