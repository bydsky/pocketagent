"""Agent abstraction: an AI coding assistant backend (Claude Code, Gemini CLI, tmux, ...)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Sequence

from .types import Event, FileAttachment, ImageAttachment


class AgentSession(ABC):
    """A running interactive session with a persistent or per-turn agent process."""

    @abstractmethod
    async def send(
        self,
        prompt: str,
        images: Sequence[ImageAttachment] = (),
        files: Sequence[FileAttachment] = (),
    ) -> None:
        """Send a user message (with optional attachments) to the agent."""

    @abstractmethod
    def events(self) -> AsyncIterator[Event]:
        """Async iterator of events emitted by the agent for the current/next turn."""

    @abstractmethod
    def alive(self) -> bool:
        """True if the underlying process/session is still usable."""

    @abstractmethod
    async def close(self) -> None:
        """Terminate the session and any underlying process."""

    @property
    def current_session_id(self) -> str | None:
        """Agent-managed session id for conversation continuity, if known."""
        return None


class Agent(ABC):
    """Factory for sessions backed by a specific agent CLI/tool."""

    name: str

    @abstractmethod
    async def start_session(
        self,
        session_id: str | None,
        work_dir: str,
        platform_system_prompt: str = "",
        show_footer: bool = False,
    ) -> AgentSession:
        """Create or resume an interactive session rooted at work_dir.

        session_id is the previously persisted agent-side session id (if any),
        used to resume a prior conversation. None starts a fresh session.
        platform_system_prompt, if set, comes from the platform's configured
        platform_system_prompt and is combined with this agent's own
        agent_system_prompt (a constructor-time option, set per agent in
        config); backends with no way to apply either to a session may
        ignore them. show_footer mirrors the resolved route's show_footer
        (see core/router.py): backends that fetch extra usage data purely to
        populate the reply footer (e.g. claude_code's rate-limit lookup)
        should skip that work when the channel isn't configured to show it;
        backends with no such cost can ignore it.
        """

    async def stop(self) -> None:
        """Release any agent-wide resources. Default: no-op."""
