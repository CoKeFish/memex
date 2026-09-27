"""Smoke tests del CLI `memex-filters`.

Invocan `main()` directamente con args y verifican efectos en DB / stdout.
La DB de test ya está provisionada por la fixture autouse de conftest.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from memex.cli.filters import main


def _capture(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    rc = main(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_add_creates_rule_and_prints_id(capsys: pytest.CaptureFixture[str], conn: Any) -> None:
    rc, out, _ = _capture(
        [
            "add",
            "--user-id",
            "1",
            "--source-type",
            "imap",
            "--scope",
            '{"from": {"equals": "spam@x"}}',
            "--action",
            "ignore",
            "--priority",
            "150",
        ],
        capsys,
    )
    assert rc == 0
    assert "created rule id=" in out
    # Verify it's in DB.
    n = conn.execute(
        text("SELECT count(*) FROM filter_rules WHERE user_id = 1 AND priority = 150")
    ).scalar()
    assert n == 1


def test_add_rejects_invalid_json_scope(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "add",
                "--user-id",
                "1",
                "--scope",
                "not-json",
                "--action",
                "ignore",
            ]
        )


def test_list_shows_inserted_rule(capsys: pytest.CaptureFixture[str]) -> None:
    main(
        [
            "add",
            "--user-id",
            "1",
            "--source-type",
            "imap",
            "--scope",
            '{"from": {"equals": "spam@x"}}',
            "--action",
            "ignore",
        ]
    )
    capsys.readouterr()  # discard add output
    rc, out, _ = _capture(["list", "--user-id", "1"], capsys)
    assert rc == 0
    assert "user=1" in out
    assert "type=imap" in out
    assert "action=ignore" in out


def test_disable_then_enable_toggles_flag(capsys: pytest.CaptureFixture[str], conn: Any) -> None:
    main(
        [
            "add",
            "--user-id",
            "1",
            "--scope",
            '{"from": {"equals": "x"}}',
            "--action",
            "ignore",
        ]
    )
    capsys.readouterr()
    rule_id = conn.execute(text("SELECT id FROM filter_rules ORDER BY id DESC LIMIT 1")).scalar()

    rc, _, _ = _capture(["disable", str(rule_id)], capsys)
    assert rc == 0
    enabled = conn.execute(
        text("SELECT enabled FROM filter_rules WHERE id = :id"), {"id": rule_id}
    ).scalar()
    assert enabled is False

    rc, _, _ = _capture(["enable", str(rule_id)], capsys)
    assert rc == 0
    enabled = conn.execute(
        text("SELECT enabled FROM filter_rules WHERE id = :id"), {"id": rule_id}
    ).scalar()
    assert enabled is True


def test_remove_deletes_rule(capsys: pytest.CaptureFixture[str], conn: Any) -> None:
    main(
        [
            "add",
            "--user-id",
            "1",
            "--scope",
            '{"from": {"equals": "x"}}',
            "--action",
            "ignore",
        ]
    )
    capsys.readouterr()
    rule_id = conn.execute(text("SELECT id FROM filter_rules ORDER BY id DESC LIMIT 1")).scalar()

    rc, _, _ = _capture(["remove", str(rule_id)], capsys)
    assert rc == 0
    n = conn.execute(
        text("SELECT count(*) FROM filter_rules WHERE id = :id"), {"id": rule_id}
    ).scalar()
    assert n == 0


def test_test_subcommand_shows_match(capsys: pytest.CaptureFixture[str]) -> None:
    main(
        [
            "add",
            "--user-id",
            "1",
            "--source-type",
            "imap",
            "--scope",
            '{"from": {"equals": "spam@x"}}',
            "--action",
            "ignore",
        ]
    )
    capsys.readouterr()

    rc, out, _ = _capture(
        [
            "test",
            "--user-id",
            "1",
            "--source-type",
            "imap",
            "--payload",
            '{"from": "spam@x"}',
        ],
        capsys,
    )
    assert rc == 0
    assert "matched rule" in out
    assert "WOULD BE DROPPED" in out


def test_test_subcommand_shows_no_match(capsys: pytest.CaptureFixture[str]) -> None:
    main(
        [
            "add",
            "--user-id",
            "1",
            "--source-type",
            "imap",
            "--scope",
            '{"from": {"equals": "spam@x"}}',
            "--action",
            "ignore",
        ]
    )
    capsys.readouterr()

    rc, out, _ = _capture(
        [
            "test",
            "--user-id",
            "1",
            "--source-type",
            "imap",
            "--payload",
            '{"from": "ok@x"}',
        ],
        capsys,
    )
    assert rc == 0
    assert "no rule matched" in out
