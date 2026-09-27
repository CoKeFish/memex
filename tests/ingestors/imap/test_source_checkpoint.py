from __future__ import annotations

from datetime import UTC, datetime

from memex.core.cursors import FolderState, ImapCursor
from memex.core.source import SourceRecord
from memex.ingestors.imap.config import ImapConfig
from memex.ingestors.imap.source import ImapSource


def _make_source() -> ImapSource:
    cfg = ImapConfig(
        server="imap.example.com",
        port=993,
        username="alice@example.com",
        auth_method="basic",
        folders=["INBOX"],
        password="x",
    )
    return ImapSource(cfg)


def _record(folder: str, uidvalidity: int, uid: int) -> SourceRecord:
    return SourceRecord(
        external_id=f"imap:imap.example.com:{uidvalidity}:{uid}",
        occurred_at=datetime(2026, 5, 26, 10, 0, tzinfo=UTC),
        payload={"folder": folder},
        dedupe_keys=[],
    )


def test_advance_checkpoint_initializes_folder_entry() -> None:
    source = _make_source()
    last = _record("INBOX", uidvalidity=17, uid=42)

    new_cp = source.advance_checkpoint(ImapCursor(), last)

    assert isinstance(new_cp, ImapCursor)
    assert new_cp.folders == {"INBOX": FolderState(uidvalidity=17, last_uid=42)}


def test_advance_checkpoint_updates_existing_folder_entry() -> None:
    source = _make_source()
    existing = ImapCursor(folders={"INBOX": FolderState(uidvalidity=17, last_uid=30)})
    last = _record("INBOX", uidvalidity=17, uid=42)

    new_cp = source.advance_checkpoint(existing, last)

    assert new_cp.folders["INBOX"] == FolderState(uidvalidity=17, last_uid=42)


def test_advance_checkpoint_adds_new_folder_without_touching_others() -> None:
    source = _make_source()
    existing = ImapCursor(folders={"INBOX": FolderState(uidvalidity=17, last_uid=99)})
    last = _record("Sent", uidvalidity=9, uid=5)

    new_cp = source.advance_checkpoint(existing, last)

    assert new_cp.folders["INBOX"] == FolderState(uidvalidity=17, last_uid=99)
    assert new_cp.folders["Sent"] == FolderState(uidvalidity=9, last_uid=5)


def test_advance_checkpoint_ignores_record_without_folder() -> None:
    source = _make_source()
    bad_record = SourceRecord(
        external_id="imap:imap.example.com:17:42",
        occurred_at=datetime(2026, 5, 26, 10, 0, tzinfo=UTC),
        payload={},  # no folder
        dedupe_keys=[],
    )
    existing = ImapCursor()

    new_cp = source.advance_checkpoint(existing, bad_record)

    # Bad record left the cursor unchanged.
    assert new_cp is existing


def test_advance_checkpoint_ignores_malformed_external_id() -> None:
    source = _make_source()
    bad_record = SourceRecord(
        external_id="not-imap-shape",
        occurred_at=datetime(2026, 5, 26, 10, 0, tzinfo=UTC),
        payload={"folder": "INBOX"},
        dedupe_keys=[],
    )
    existing = ImapCursor()

    new_cp = source.advance_checkpoint(existing, bad_record)

    # Malformed external_id left the cursor unchanged.
    assert new_cp is existing


def test_advance_checkpoint_is_monotonic_within_uidvalidity() -> None:
    """Un record con uid MENOR (llegada desordenada / corridas solapadas) no retrocede el cursor."""
    source = _make_source()
    existing = ImapCursor(folders={"INBOX": FolderState(uidvalidity=17, last_uid=42)})
    last = _record("INBOX", uidvalidity=17, uid=30)

    new_cp = source.advance_checkpoint(existing, last)

    assert new_cp.folders["INBOX"] == FolderState(uidvalidity=17, last_uid=42)


def test_advance_checkpoint_resets_on_uidvalidity_change() -> None:
    """Si cambia la uidvalidity (carpeta recreada / migración) el cursor arranca fresco en el uid
    nuevo, sin tomar el max contra el last_uid de la uidvalidity vieja."""
    source = _make_source()
    existing = ImapCursor(folders={"INBOX": FolderState(uidvalidity=17, last_uid=99)})
    last = _record("INBOX", uidvalidity=18, uid=3)

    new_cp = source.advance_checkpoint(existing, last)

    assert new_cp.folders["INBOX"] == FolderState(uidvalidity=18, last_uid=3)
