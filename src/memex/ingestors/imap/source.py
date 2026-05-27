from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from memex.core.source import SourceRecord
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
    """IMAP Source — implements memex.core.source.Source Protocol.

    One ImapSource = one IMAP account (one set of credentials), processing
    one or more folders. The cursor JSONB has shape:

        {"folders": {"INBOX": {"uidvalidity": 17, "last_uid": 12345}, ...}}
    """

    type = "imap"

    def __init__(self, cfg: ImapConfig) -> None:
        self.cfg = cfg
        self._log = get_logger("memex.ingestors.imap.source").bind(server=cfg.server)

    def fetch(self, checkpoint: dict[str, Any] | None) -> Iterable[SourceRecord]:
        folders_cp: dict[str, dict[str, int]] = (checkpoint or {}).get("folders", {}) or {}
        since_date = datetime.now(UTC) - timedelta(days=self.cfg.since_days)

        with ImapClient(self.cfg) as client:
            for folder in self.cfg.folders:
                folder_log = self._log.bind(folder=folder)
                current_uidvalidity = client.folder_uidvalidity(folder)
                folder_cp = folders_cp.get(folder, {})
                stored_uidvalidity = folder_cp.get("uidvalidity")
                last_uid = int(folder_cp.get("last_uid", 0))

                if (
                    stored_uidvalidity is not None
                    and int(stored_uidvalidity) != current_uidvalidity
                ):
                    folder_log.warning(
                        "uidvalidity_changed",
                        old=stored_uidvalidity,
                        new=current_uidvalidity,
                    )
                    last_uid = 0

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

    def advance_checkpoint(
        self, checkpoint: dict[str, Any] | None, last: SourceRecord
    ) -> dict[str, Any]:
        cp: dict[str, Any] = dict(checkpoint or {})
        cp_folders: dict[str, dict[str, int]] = dict(cp.get("folders", {}) or {})

        folder = last.payload.get("folder")
        if not folder or not isinstance(folder, str):
            return cp

        # external_id shape: imap:{server}:{uidvalidity}:{uid}
        parts = last.external_id.split(":")
        if len(parts) < 4 or parts[0] != "imap":
            return cp
        try:
            uidvalidity = int(parts[-2])
            uid = int(parts[-1])
        except ValueError:
            return cp

        cp_folders[folder] = {"uidvalidity": uidvalidity, "last_uid": uid}
        cp["folders"] = cp_folders
        return cp

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
