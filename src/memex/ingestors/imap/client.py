from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import AbstractContextManager
from datetime import datetime
from types import TracebackType
from typing import Any

from imap_tools import MailBox, MailBoxUnencrypted

from memex.ingestors.imap import oauth
from memex.ingestors.imap.config import ImapConfig
from memex.logging import get_logger


class ImapClient(AbstractContextManager["ImapClient"]):
    """Thin wrapper over imap_tools.MailBox.

    Encapsulates login/logout (basic or XOAUTH2), folder selection, UIDVALIDITY
    lookup, and incremental fetch since last UID. All IMAP-specific knowledge
    stays here.
    """

    def __init__(self, cfg: ImapConfig) -> None:
        self.cfg = cfg
        self._log = get_logger("memex.ingestors.imap.client").bind(
            server=cfg.server,
            username=cfg.username,
            auth_method=cfg.auth_method,
        )
        self._mailbox: MailBox | MailBoxUnencrypted | None = None

    def __enter__(self) -> ImapClient:
        if self.cfg.use_ssl:
            mb: MailBox | MailBoxUnencrypted = MailBox(self.cfg.server, port=self.cfg.port)
        else:
            mb = MailBoxUnencrypted(self.cfg.server, port=self.cfg.port)

        if self.cfg.auth_method == "basic":
            self._mailbox = mb.login(self.cfg.username, self.cfg.password)
        elif self.cfg.auth_method == "oauth2":
            provider = oauth.resolve(self.cfg.oauth_provider)
            access_token = provider.get_access_token(token_path=self.cfg.oauth_token_path)
            self._mailbox = mb.xoauth2(self.cfg.username, access_token)
        else:
            raise RuntimeError(f"unsupported auth_method: {self.cfg.auth_method!r}")

        self._log.info("imap_login_ok")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._mailbox is not None:
            try:
                self._mailbox.logout()
            except Exception as e:
                self._log.warning("imap_logout_failed", exc=str(e))
            finally:
                self._mailbox = None

    def _require_mailbox(self) -> MailBox | MailBoxUnencrypted:
        if self._mailbox is None:
            raise RuntimeError("ImapClient must be used as a context manager")
        return self._mailbox

    def folder_uidvalidity(self, folder: str) -> int:
        mb = self._require_mailbox()
        status = mb.folder.status(folder, ("UIDVALIDITY",))
        return int(status["UIDVALIDITY"])

    def fetch_since_uid(
        self,
        folder: str,
        last_uid: int,
        *,
        since_date: datetime,
        batch_size: int = 50,
    ) -> Iterator[Any]:
        """Yield imap_tools.MailMessage objects from `folder` newer than `last_uid`.

        If `last_uid == 0` (first fetch), uses SINCE `since_date` as the
        criteria. Otherwise uses `UID {last_uid+1}:*`. Limited to `batch_size`
        per call.
        """
        mb = self._require_mailbox()
        mb.folder.set(folder)

        if last_uid > 0:
            criteria = f"UID {last_uid + 1}:*"
        else:
            criteria = f'SINCE {since_date.strftime("%d-%b-%Y")}'

        self._log.info(
            "imap_fetch_start",
            folder=folder,
            criteria=criteria,
            batch_size=batch_size,
        )

        yielded = 0
        started = time.monotonic()
        for message in mb.fetch(criteria=criteria, mark_seen=False, limit=batch_size, bulk=True):
            yield message
            yielded += 1

        self._log.info(
            "imap_fetch_end",
            folder=folder,
            yielded=yielded,
            ms_elapsed=int((time.monotonic() - started) * 1000),
        )
