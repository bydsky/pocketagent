"""Caches live agent sessions and persists their resume ids across restarts."""

from __future__ import annotations

import json
from pathlib import Path

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

    def set_resume_id(self, session_key: str, agent_session_id: str | None) -> None:
        if not agent_session_id:
            return
        if self._resume_ids.get(session_key) == agent_session_id:
            return
        self._resume_ids[session_key] = agent_session_id
        self._save()

    async def get_or_create(
        self, session_key: str, agent: Agent, work_dir: str
    ) -> AgentSession:
        existing = self._live.get(session_key)
        if existing is not None and existing.alive():
            return existing

        resume_id = self._resume_ids.get(session_key)
        session = await agent.start_session(resume_id, work_dir)
        self._live[session_key] = session
        return session

    async def close_all(self) -> None:
        for session in self._live.values():
            await session.close()
        self._live.clear()
