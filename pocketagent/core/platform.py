"""Platform abstraction: a messaging surface (Discord, Slack, Telegram, ...)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

from .types import Message

MessageHandler = Callable[["Platform", Message], Awaitable[None]]


class Platform(ABC):
    """A messaging platform that delivers incoming messages and sends replies."""

    name: str

    @abstractmethod
    async def start(self, handler: MessageHandler) -> None:
        """Connect to the platform and begin dispatching messages to handler."""

    @abstractmethod
    async def reply(self, reply_ctx: Any, content: str) -> None:
        """Reply to the message that produced reply_ctx."""

    @abstractmethod
    async def send(self, reply_ctx: Any, content: str) -> None:
        """Send a new (non-reply) message into the same channel as reply_ctx."""

    @abstractmethod
    async def stop(self) -> None:
        """Disconnect from the platform."""


def allow_list(allow_from: str, user_id: str) -> bool:
    """Check whether user_id is permitted based on a comma-separated allow_from string.

    Empty or "*" means allow all. Comparison is case-insensitive.
    """
    allow_from = allow_from.strip()
    if allow_from == "" or allow_from == "*":
        return True
    return any(
        part.strip().lower() == user_id.lower() for part in allow_from.split(",")
    )
