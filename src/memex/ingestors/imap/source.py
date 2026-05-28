from __future__ import annotations

import asyncio
from builtins import type as _type
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Literal, Protocol

from pydantic import BaseModel

from memex.core.cursors import FolderState, ImapCursor
from memex.core.payloads import BasePayload, EmailPayload
from memex.core.source import HealthResult, Source, SourceKind, SourceRecord
from memex.ingestors.imap.client import ImapClient
from memex.ingestors.imap.config import ImapConfig
from memex.ingestors.imap.parser import parse_email_message
from memex.logging import get_logger


class _MailMessageLike(Protocol):
    """Subset of imap_tools.MailMessage we actually use.

    Documented as a Protocol for explicitness and to keep tests substitutable.
    """

    uid: str | None
    flags: tuple[str, ...]
    date: datetime | None
    size: int
    obj: Any  # underlying email.message.Message


class ImapSource:
    """IMAP Source — implements memex.core.source.Source[ImapCursor].

    One ImapSource = one IMAP account (one set of credentials), processing
    one or more folders. The cursor is `ImapCursor` (Pydantic); the runner
    handles the JSONB ↔ ImapCursor conversion at the wire boundary, so this
    class only ever sees the typed value.
    """

    type: ClassVar[str] = "imap"
    kind: ClassVar[SourceKind] = SourceKind.EMAIL
    payload_schema: ClassVar[_type[BasePayload]] = EmailPayload
    config_schema: ClassVar[_type[BaseModel]] = ImapConfig
    checkpoint_schema: ClassVar[_type[BaseModel]] = ImapCursor

    def __init__(self, cfg: ImapConfig) -> None:
        self.cfg = cfg
        self._log = get_logger("memex.ingestors.imap.source").bind(server=cfg.server)

    async def health_check(self) -> HealthResult:
        """Verify IMAP auth + connectivity by opening + closing a session.

        Runs the blocking imap_tools work in a threadpool. Never raises —
        any error becomes `status="unhealthy"` with the exception text.
        """

        def _probe() -> tuple[Literal["healthy", "unhealthy"], str]:
            try:
                with ImapClient(self.cfg) as client:
                    # Touching one folder forces a real round-trip beyond login.
                    folder = self.cfg.folders[0] if self.cfg.folders else "INBOX"
                    client.folder_uidvalidity(folder)
                return ("healthy", f"login ok, folder {folder!r} reachable")
            except Exception as e:
                return ("unhealthy", f"{type(e).__name__}: {e}")

        status, detail = await asyncio.to_thread(_probe)
        return HealthResult(
            status=status,
            detail=detail,
            checked_at=datetime.now(UTC),
        )

    def fetch(self, checkpoint: ImapCursor) -> Iterable[SourceRecord]:
        since_date = datetime.now(UTC) - timedelta(days=self.cfg.since_days)

        with ImapClient(self.cfg) as client:
            for folder in self.cfg.folders:
                folder_log = self._log.bind(folder=folder)
                current_uidvalidity = client.folder_uidvalidity(folder)
                folder_state = checkpoint.folders.get(folder)

                if folder_state is not None and folder_state.uidvalidity != current_uidvalidity:
                    folder_log.warning(
                        "uidvalidity_changed",
                        old=folder_state.uidvalidity,
                        new=current_uidvalidity,
                    )
                    last_uid = 0
                else:
                    last_uid = folder_state.last_uid if folder_state else 0

                folder_log.info(
                    "folder_fetch_start",
                    last_uid=last_uid,
                    uidvalidity=current_uidvalidity,
                )

                count = 0
                for mailmsg in client.fetch_since_uid(
                    folder,
                    last_uid,
                    since_date=since_date,
                    batch_size=self.cfg.batch_size,
                ):
                    yield self._mailmsg_to_record(mailmsg, folder, current_uidvalidity)
                    count += 1

                folder_log.info("folder_fetch_end", count=count)

    def advance_checkpoint(self, checkpoint: ImapCursor, last: SourceRecord) -> ImapCursor:
        folder = last.payload.get("folder")
        if not folder or not isinstance(folder, str):
            return checkpoint

        # external_id shape: imap:{server}:{uidvalidity}:{uid}
        parts = last.external_id.split(":")
        if len(parts) < 4 or parts[0] != "imap":
            return checkpoint
        try:
            uidvalidity = int(parts[-2])
            uid = int(parts[-1])
        except ValueError:
            return checkpoint

        new_folders = dict(checkpoint.folders)
        new_folders[folder] = FolderState(uidvalidity=uidvalidity, last_uid=uid)
        return ImapCursor(folders=new_folders)

    def _mailmsg_to_record(
        self,
        mailmsg: _MailMessageLike,
        folder: str,
        uidvalidity: int,
    ) -> SourceRecord:
        uid_str = mailmsg.uid or "0"
        try:
            uid = int(uid_str)
        except ValueError:
            uid = 0
        flags = list(mailmsg.flags or ())
        size_bytes = int(getattr(mailmsg, "size", 0) or 0)
        internaldate = mailmsg.date
        if internaldate is None:
            internaldate = datetime.now(UTC)
        elif internaldate.tzinfo is None:
            internaldate = internaldate.replace(tzinfo=UTC)

        return parse_email_message(
            mailmsg.obj,
            server=self.cfg.server,
            folder=folder,
            uidvalidity=uidvalidity,
            uid=uid,
            internaldate=internaldate,
            flags=flags,
            size_bytes=size_bytes,
            max_body_bytes=self.cfg.max_body_bytes,
            fetch_body=self.cfg.fetch_body,
        )


def make_source(cfg: dict[str, Any]) -> Source[Any]:
    """SourceFactory for IMAP — validates config dict and returns an ImapSource.

    Matches the `SourceFactory` Protocol; this is what the registry returns when
    `resolve("imap")` is called.
    """
    imap_cfg = ImapConfig.from_source_config(cfg)
    return ImapSource(imap_cfg)
