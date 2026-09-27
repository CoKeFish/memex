"""Canales de entrega: por dónde le llega un aviso al usuario (Telegram, log; después push, correo).

Contrato `Channel`: `name` y `send(target, text, notification) -> referencia externa`. El servicio
arma el texto (título + cuerpo + enlace) y el canal solo lo transporta. Registro: `_FACTORIES`
nombre → fábrica que recibe el env del daemon (credenciales). Agregar un canal = un módulo acá y
una línea en el registro; la tabla `notification_channels` guarda las suscripciones del usuario.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from memex.notifications.client import Notification
from memex.notifications.settings import NotificationsEnv


class ChannelError(Exception):
    """El canal no pudo construirse o enviar (credenciales ausentes, API caída)."""


@runtime_checkable
class Channel(Protocol):
    name: str

    async def send(self, target: str, text: str, notification: Notification) -> str | None:
        """Entrega `text` a `target`; devuelve una referencia externa (id del mensaje) o None.
        Lanza `ChannelError` si falla."""
        ...

    async def aclose(self) -> None: ...


ChannelFactory = Callable[[NotificationsEnv], Channel]


def _telegram(env: NotificationsEnv) -> Channel:
    from memex.notifications.channels.telegram import TelegramChannel

    return TelegramChannel(env.telegram_bot_token)


def _log(env: NotificationsEnv) -> Channel:
    from memex.notifications.channels.log import LogChannel

    return LogChannel()


_FACTORIES: dict[str, ChannelFactory] = {"telegram": _telegram, "log": _log}


def channel_names() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def build_channel(name: str, env: NotificationsEnv) -> Channel:
    """Instancia el canal `name` con las credenciales del env. Nombre desconocido → ChannelError."""
    factory = _FACTORIES.get(name)
    if factory is None:
        raise ChannelError(f"canal desconocido: {name!r}; válidos: {channel_names()}")
    return factory(env)


__all__ = ["Channel", "ChannelError", "ChannelFactory", "build_channel", "channel_names"]
