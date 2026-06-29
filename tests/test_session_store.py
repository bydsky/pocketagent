from typing import AsyncIterator

import pytest

from pocketagent.core.agent import Agent, AgentSession
from pocketagent.core.session_store import SessionStore
from pocketagent.core.types import Event, EventType


class _FakeAgentSession(AgentSession):
    def __init__(self):
        self.closed = False

    async def send(self, prompt, images=(), files=()):
        pass

    async def events(self) -> AsyncIterator[Event]:
        yield Event(type=EventType.RESULT, content="ok", done=True)

    def alive(self) -> bool:
        return not self.closed

    async def close(self) -> None:
        self.closed = True


class _FakeAgent(Agent):
    name = "fake"

    async def start_session(
        self, session_id, work_dir, platform_system_prompt="", show_footer=False
    ) -> AgentSession:
        return _FakeAgentSession()


@pytest.mark.asyncio
async def test_clear_all_closes_live_sessions(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    agent = _FakeAgent()
    session = await store.get_or_create("k1", agent, str(tmp_path))

    await store.clear_all()

    assert session.closed is True
    assert store._live == {}


@pytest.mark.asyncio
async def test_clear_all_forgets_persisted_resume_ids(tmp_path):
    state_path = tmp_path / "sessions.json"
    store = SessionStore(state_path)
    store.set_resume_id("k1", "resume-abc")

    await store.clear_all()

    assert store._resume_ids == {}
    # Reloading from disk must not resurrect the cleared resume id.
    reloaded = SessionStore(state_path)
    assert reloaded._resume_ids == {}


@pytest.mark.asyncio
async def test_clear_matching_only_clears_keys_matched_by_predicate(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    agent = _FakeAgent()
    session_a = await store.get_or_create("discord:111:1", agent, str(tmp_path))
    session_b = await store.get_or_create("discord:222:1", agent, str(tmp_path))
    store.set_resume_id("discord:111:1", "resume-a")
    store.set_resume_id("discord:222:1", "resume-b")

    await store.clear_matching(lambda key: key.startswith("discord:111:"))

    assert session_a.closed is True
    assert session_b.closed is False
    assert "discord:111:1" not in store._live
    assert "discord:222:1" in store._live
    assert store._resume_ids == {"discord:222:1": "resume-b"}


@pytest.mark.asyncio
async def test_has_session_true_for_live_session(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    agent = _FakeAgent()
    await store.get_or_create("k1", agent, str(tmp_path))

    assert store.has_session("k1") is True
    assert store.has_session("k2") is False


def test_has_session_true_for_persisted_resume_id_without_live_session(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    store.set_resume_id("k1", "resume-abc")

    assert store.has_session("k1") is True


@pytest.mark.asyncio
async def test_clear_all_then_get_or_create_starts_fresh_session(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    agent = _FakeAgent()
    store.set_resume_id("k1", "resume-abc")

    await store.clear_all()

    captured: dict[str, str | None] = {}

    class _CapturingAgent(_FakeAgent):
        async def start_session(
            self, session_id, work_dir, platform_system_prompt="", show_footer=False
        ) -> AgentSession:
            captured["session_id"] = session_id
            return _FakeAgentSession()

    await store.get_or_create("k1", _CapturingAgent(), str(tmp_path))

    assert captured["session_id"] is None
