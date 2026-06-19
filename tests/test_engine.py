from typing import AsyncIterator

import pytest

from pocketagent.core.agent import Agent, AgentSession
from pocketagent.core.commands import CommandRegistry
from pocketagent.core.engine import Engine
from pocketagent.core.platform import Platform
from pocketagent.core.router import Router
from pocketagent.core.session_store import SessionStore
from pocketagent.core.types import Event, EventType, Message
from pocketagent.core.workspace import WorkspaceManager


class _FakeAgentSession(AgentSession):
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, prompt, images=(), files=()):
        self.sent.append(prompt)

    async def events(self) -> AsyncIterator[Event]:
        yield Event(type=EventType.RESULT, content="ok", done=True)

    def alive(self) -> bool:
        return True

    async def close(self) -> None:
        pass


class _FakeAgent(Agent):
    name = "fake"

    async def start_session(self, session_id, work_dir, platform_system_prompt="") -> AgentSession:
        self.last_platform_system_prompt = platform_system_prompt
        return _FakeAgentSession()


class _FakePlatform(Platform):
    name = "fake"

    def __init__(self):
        self.replies: list[str] = []
        self.typing_active = False
        self.typing_was_active_during_reply = None

    async def start(self, handler):
        pass

    async def reply(self, reply_ctx, content: str) -> None:
        self.typing_was_active_during_reply = self.typing_active
        self.replies.append(content)

    async def send(self, reply_ctx, content: str) -> None:
        self.replies.append(content)

    async def stop(self) -> None:
        pass

    def typing(self, reply_ctx):
        platform = self

        class _Typing:
            async def __aenter__(self):
                platform.typing_active = True
                return self

            async def __aexit__(self, *exc):
                platform.typing_active = False
                return False

        return _Typing()


def _make_engine(
    tmp_path, platform_system_prompt: str = "", show_footer: bool = True
) -> tuple[Engine, _FakeAgent]:
    agent = _FakeAgent()
    workspace = WorkspaceManager(tmp_path / "workspace")
    router = Router(
        default_agent="fake",
        workspace=workspace,
        platform_system_prompt=platform_system_prompt,
        show_footer=show_footer,
    )
    session_store = SessionStore(tmp_path / "sessions.json")
    engine = Engine(
        agents={"fake": agent},
        routers={"fake": router},
        session_store=session_store,
        commands=CommandRegistry(),
    )
    return engine, agent


def _make_message() -> Message:
    return Message(
        session_key="fake:1:1",
        platform="fake",
        channel_id="1",
        user_id="1",
        user_name="u",
        content="hello",
    )


@pytest.mark.asyncio
async def test_on_message_shows_typing_while_agent_works(tmp_path):
    engine, _ = _make_engine(tmp_path)
    platform = _FakePlatform()

    await engine.on_message(platform, _make_message())

    assert platform.replies == ["ok"]
    # typing must have been active while waiting on the agent...
    assert platform.typing_was_active_during_reply is True
    # ...and turned off again once the whole exchange is done.
    assert platform.typing_active is False


@pytest.mark.asyncio
async def test_on_message_typing_exits_even_on_handler_error(tmp_path, monkeypatch):
    engine, _ = _make_engine(tmp_path)
    platform = _FakePlatform()
    msg = _make_message()

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(engine.session_store, "get_or_create", boom)

    await engine.on_message(platform, msg)

    assert platform.typing_active is False
    assert platform.replies == ["Sorry, something went wrong handling that message."]


@pytest.mark.asyncio
async def test_on_message_passes_platform_system_prompt_to_agent(tmp_path):
    engine, agent = _make_engine(tmp_path, platform_system_prompt="You are operating via chat.")
    platform = _FakePlatform()

    await engine.on_message(platform, _make_message())

    assert agent.last_platform_system_prompt == "You are operating via chat."


@pytest.mark.asyncio
async def test_on_message_appends_footer_when_result_has_usage_data(tmp_path):
    class _AgentSessionWithUsage(_FakeAgentSession):
        async def events(self) -> AsyncIterator[Event]:
            yield Event(
                type=EventType.RESULT,
                content="ok",
                done=True,
                input_tokens=10,
                output_tokens=4,
                model="claude-sonnet-4-6",
                cost_usd=0.0533424,
                context_used_pct=14,
                rate_limit_5h_pct=40,
                rate_limit_7d_pct=17,
            )

    class _AgentWithUsage(_FakeAgent):
        async def start_session(self, session_id, work_dir, platform_system_prompt="") -> AgentSession:
            return _AgentSessionWithUsage()

    engine, _ = _make_engine(tmp_path)
    engine.agents["fake"] = _AgentWithUsage()
    platform = _FakePlatform()

    await engine.on_message(platform, _make_message())

    # event.model is already display-formatted by the agent backend that set it
    # (here, a raw test value) -- the engine just passes it through unchanged.
    assert platform.replies == ["ok\n\n· claude-sonnet-4-6 · 14 tokens · ctx:14% · 5h:40% · 7d:17% · $0.0533"]
