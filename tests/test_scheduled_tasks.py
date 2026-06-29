from typing import AsyncIterator

import pytest

from pocketagent.core.agent import Agent, AgentSession
from pocketagent.core.commands import CommandRegistry
from pocketagent.core.engine import Engine
from pocketagent.core.platform import Platform
from pocketagent.core.router import Router
from pocketagent.core.scheduled_tasks import run_scheduled_task
from pocketagent.core.session_store import SessionStore
from pocketagent.core.types import Event, EventType
from pocketagent.core.workspace import WorkspaceManager


class _FakeAgentSession(AgentSession):
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, prompt, images=(), files=()):
        self.sent.append(prompt)

    async def events(self) -> AsyncIterator[Event]:
        yield Event(type=EventType.RESULT, content="summary", done=True)

    def alive(self) -> bool:
        return True

    async def close(self) -> None:
        pass


class _FakeAgent(Agent):
    name = "fake"

    async def start_session(
        self, session_id, work_dir, platform_system_prompt="", show_footer=False
    ) -> AgentSession:
        return _FakeAgentSession()


class _FakePlatform(Platform):
    name = "fake"

    def __init__(self, channel_ctx="channel-ctx"):
        self.replies: list[str] = []
        self._channel_ctx = channel_ctx
        self.make_channel_ctx_calls: list[str] = []

    async def start(self, handler):
        pass

    async def reply(self, reply_ctx, content: str) -> None:
        self.replies.append(content)

    async def send(self, reply_ctx, content: str) -> None:
        self.replies.append(content)

    async def stop(self) -> None:
        pass

    async def make_channel_ctx(self, channel_id: str):
        self.make_channel_ctx_calls.append(channel_id)
        return self._channel_ctx


class _UnsupportedPlatform(_FakePlatform):
    async def make_channel_ctx(self, channel_id: str):
        raise NotImplementedError("fake: proactive channel sends not supported")


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


@pytest.mark.asyncio
async def test_skips_when_no_existing_session(tmp_path):
    engine = _make_engine(tmp_path)
    platform = _FakePlatform()

    await run_scheduled_task(engine, platform, "111", "222", "summarize vocab")

    assert platform.replies == []
    assert platform.make_channel_ctx_calls == []


@pytest.mark.asyncio
async def test_sends_prompt_and_posts_reply_when_session_exists(tmp_path):
    engine = _make_engine(tmp_path)
    platform = _FakePlatform()
    engine.session_store.set_resume_id("fake:111:222", "resume-abc")

    await run_scheduled_task(engine, platform, "111", "222", "summarize vocab")

    assert platform.replies == ["summary"]
    assert platform.make_channel_ctx_calls == ["111"]


@pytest.mark.asyncio
async def test_skips_quietly_when_platform_does_not_support_proactive_sends(tmp_path):
    engine = _make_engine(tmp_path)
    platform = _UnsupportedPlatform()
    engine.session_store.set_resume_id("fake:111:222", "resume-abc")

    await run_scheduled_task(engine, platform, "111", "222", "summarize vocab")

    assert platform.replies == []
