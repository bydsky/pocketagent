"""Caches live agent sessions and persists their resume ids across restarts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .agent import Agent, AgentSession


class SessionStore:
    def __init__(self, state_path: str | Path) -> None:
        self._state_path = Path(state_path)
        self._live: dict[str, AgentSession] = {}
        self._resume_ids: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self._state_path.exists():
            try:
                self._resume_ids = json.loads(self._state_path.read_text())
            except (json.JSONDecodeError, OSError):
                self._resume_ids = {}

    def _save(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(self._resume_ids, indent=2))

    def has_session(self, session_key: str) -> bool:
        """Whether session_key has ever had a turn -- live or persisted resume
        id. Used by scheduled tasks to skip firing a prompt into a channel
        that has no conversation yet (a fresh, unresumed session would have
        nothing to act on)."""

        return session_key in self._live or session_key in self._resume_ids

    def set_resume_id(self, session_key: str, agent_session_id: str | None) -> None:
        if not agent_session_id:
            return
        if self._resume_ids.get(session_key) == agent_session_id:
            return
        self._resume_ids[session_key] = agent_session_id
        self._save()

    async def get_or_create(
        self,
        session_key: str,
        agent: Agent,
        work_dir: str,
        platform_system_prompt: str = "",
        show_footer: bool = False,
    ) -> AgentSession:
        existing = self._live.get(session_key)
        if existing is not None and existing.alive():
            return existing

        resume_id = self._resume_ids.get(session_key)
        session = await agent.start_session(resume_id, work_dir, platform_system_prompt, show_footer)
        self._live[session_key] = session
        return session

    async def close_all(self) -> None:
        for session in self._live.values():
            await session.close()
        self._live.clear()

    async def clear_matching(self, predicate: Callable[[str], bool]) -> None:
        """Close every live session and forget the resume id for any session_key
        matching predicate.

        Used by the daily-reset scheduler: the next message on a cleared channel
        starts a brand-new agent session instead of resuming, mirroring what an
        interactive `/clear` does inside the agent CLI itself. A scheduler with
        a per-channel override clears only that channel's session_keys; the
        global default scheduler clears everything else.
        """

        for key in [k for k in self._live if predicate(k)]:
            await self._live.pop(key).close()

        matching_resume_keys = [k for k in self._resume_ids if predicate(k)]
        if matching_resume_keys:
            for key in matching_resume_keys:
                del self._resume_ids[key]
            self._save()

    async def clear_all(self) -> None:
        """Close every live session and forget all persisted resume ids."""

        await self.clear_matching(lambda _: True)
