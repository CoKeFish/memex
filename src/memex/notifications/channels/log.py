"""Canal `log`: deja el aviso en el log (pruebas, entornos sin Telegram, depuración)."""

from __future__ import annotations

from memex.logging import get_logger
from memex.notifications.client import Notification

_log = get_logger("memex.notifications.channels.log")


class LogChannel:
    name = "log"

    async def send(self, target: str, text: str, notification: Notification) -> str | None:
        _log.info(
            "notification.delivered.log",
            target=target,
            kind=notification.kind,
            audience=notification.audience,
            severity=notification.severity,
            text=text[:500],
        )
        return None

    async def aclose(self) -> None:
        return None
