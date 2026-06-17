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

    async def start_session(self, session_id, work_dir) -> AgentSession:
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


def _make_engine(tmp_path) -> Engine:
    agent = _FakeAgent()
    workspace = WorkspaceManager(tmp_path / "workspace")
    router = Router(default_agent="fake", workspace=workspace)
    session_store = SessionStore(tmp_path / "sessions.json")
    return Engine(
        agents={"fake": agent},
        routers={"fake": router},
        session_store=session_store,
        commands=CommandRegistry(),
    )


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
    engine = _make_engine(tmp_path)
    platform = _FakePlatform()

    await engine.on_message(platform, _make_message())

    assert platform.replies == ["ok"]
    # typing must have been active while waiting on the agent...
    assert platform.typing_was_active_during_reply is True
    # ...and turned off again once the whole exchange is done.
    assert platform.typing_active is False


@pytest.mark.asyncio
async def test_on_message_typing_exits_even_on_handler_error(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path)
    platform = _FakePlatform()
    msg = _make_message()

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(engine.session_store, "get_or_create", boom)

    await engine.on_message(platform, msg)

    assert platform.typing_active is False
    assert platform.replies == ["Sorry, something went wrong handling that message."]
