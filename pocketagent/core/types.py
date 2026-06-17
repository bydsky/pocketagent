"""Shared data types passed between platforms, the engine, and agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass
class ImageAttachment:
    mime_type: str
    data: bytes
    file_name: str = ""


@dataclass
class FileAttachment:
    mime_type: str
    data: bytes
    file_name: str = ""


@dataclass
class Message:
    """A unified incoming message from any platform."""

    session_key: str  # e.g. "discord:{channel_id}:{user_id}"
    platform: str
    channel_id: str
    user_id: str
    user_name: str
    content: str
    channel_key: str = ""  # platform channel identifier used for workspace binding
    chat_name: str = ""  # human-readable channel/chat name (for workspace folder naming)
    images: list[ImageAttachment] = field(default_factory=list)
    files: list[FileAttachment] = field(default_factory=list)
    reply_ctx: Any = None


class EventType(Enum):
    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    RESULT = "result"
    ERROR = "error"
    PERMISSION_REQUEST = "permission_request"
    THINKING = "thinking"


@dataclass
class Event:
    """A single piece of agent output streamed back to the engine."""

    type: EventType
    content: str = ""
    tool_name: str = ""
    tool_input: str = ""
    session_id: str | None = None
    request_id: str | None = None
    done: bool = False
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
