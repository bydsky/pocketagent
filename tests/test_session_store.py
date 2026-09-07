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
        self, session_id, work_dir, platform_system_prompt="", show_footer=False, model=""
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
            self, session_id, work_dir, platform_system_prompt="", show_footer=False, model=""
        ) -> AgentSession:
            captured["session_id"] = session_id
            return _FakeAgentSession()

    await store.get_or_create("k1", _CapturingAgent(), str(tmp_path))

    assert captured["session_id"] is None


class _CapturingModelAgent(_FakeAgent):
    """Records the model argument each start_session call receives."""

    def __init__(self):
        self.models: list[str] = []

    async def start_session(
        self, session_id, work_dir, platform_system_prompt="", show_footer=False, model=""
    ) -> AgentSession:
        self.models.append(model)
        return _FakeAgentSession()


@pytest.mark.asyncio
async def test_get_or_create_passes_model_override_to_agent(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    agent = _CapturingModelAgent()

    await store.get_or_create("k1", agent, str(tmp_path))
    assert agent.models == [""]  # no override set yet

    await store.set_model("k1", "opus")
    await store.get_or_create("k1", agent, str(tmp_path))
    assert agent.models == ["", "opus"]


@pytest.mark.asyncio
async def test_set_model_drops_live_session_and_resume_id(tmp_path):
    """A switch is inert unless the session is dropped: claude --resume
    restores the transcript's original model regardless of --model."""

    store = SessionStore(tmp_path / "sessions.json")
    agent = _FakeAgent()
    session = await store.get_or_create("k1", agent, str(tmp_path))
    store.set_resume_id("k1", "resume-abc")

    await store.set_model("k1", "opus")

    assert session.closed is True
    assert store._live == {}
    assert store._resume_ids == {}


@pytest.mark.asyncio
async def test_set_model_only_affects_its_own_session_key(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    agent = _FakeAgent()
    other = await store.get_or_create("k2", agent, str(tmp_path))
    store.set_resume_id("k2", "resume-other")

    await store.set_model("k1", "opus")

    assert other.closed is False
    assert store._resume_ids == {"k2": "resume-other"}
    assert store.get_model("k2") == ""


@pytest.mark.asyncio
async def test_model_override_persists_across_restart(tmp_path):
    state_path = tmp_path / "sessions.json"
    store = SessionStore(state_path)
    await store.set_model("k1", "opus")

    reloaded = SessionStore(state_path)
    assert reloaded.get_model("k1") == "opus"


@pytest.mark.asyncio
async def test_model_override_survives_a_daily_reset(tmp_path):
    """The chosen model outlives clear_all -- that's why it isn't stored
    inside sessions.json, which every daily reset wipes."""

    state_path = tmp_path / "sessions.json"
    store = SessionStore(state_path)
    await store.set_model("k1", "opus")
    store.set_resume_id("k1", "resume-abc")

    await store.clear_all()

    assert store._resume_ids == {}
    assert store.get_model("k1") == "opus"
    assert SessionStore(state_path).get_model("k1") == "opus"


@pytest.mark.asyncio
async def test_set_model_empty_clears_the_override(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    await store.set_model("k1", "opus")

    await store.set_model("k1", "")

    assert store.get_model("k1") == ""
    assert SessionStore(tmp_path / "sessions.json").get_model("k1") == ""


@pytest.mark.asyncio
async def test_corrupt_model_overrides_file_degrades_to_no_override(tmp_path):
    (tmp_path / "model_overrides.json").write_text("{not json")

    store = SessionStore(tmp_path / "sessions.json")

    assert store.get_model("k1") == ""
