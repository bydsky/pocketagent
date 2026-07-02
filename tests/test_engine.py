import asyncio
from datetime import datetime, timedelta, timezone
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

    async def start_session(
        self, session_id, work_dir, platform_system_prompt="", show_footer=False
    ) -> AgentSession:
        self.last_platform_system_prompt = platform_system_prompt
        self.last_show_footer = show_footer
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
    tmp_path,
    platform_system_prompt: str = "",
    show_footer: bool = True,
    scheduled_tasks_dir=None,
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
        scheduled_tasks_dir=scheduled_tasks_dir,
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
async def test_on_message_appends_schedule_instructions_when_configured(tmp_path):
    engine, agent = _make_engine(
        tmp_path, platform_system_prompt="You are operating via chat.", scheduled_tasks_dir=tmp_path
    )
    platform = _FakePlatform()

    await engine.on_message(platform, _make_message())

    assert agent.last_platform_system_prompt.startswith("You are operating via chat.\n\n")
    assert "schedule-task" in agent.last_platform_system_prompt


@pytest.mark.asyncio
async def test_on_message_no_schedule_instructions_when_not_configured(tmp_path):
    engine, agent = _make_engine(tmp_path, platform_system_prompt="You are operating via chat.")
    platform = _FakePlatform()

    await engine.on_message(platform, _make_message())

    assert agent.last_platform_system_prompt == "You are operating via chat."


@pytest.mark.asyncio
async def test_on_message_writes_scheduled_task_and_shows_confirmation(tmp_path):
    class _SchedulingAgentSession(_FakeAgentSession):
        async def events(self) -> AsyncIterator[Event]:
            yield Event(
                type=EventType.RESULT,
                content=(
                    "Sure, I'll check in daily.\n\n"
                    '```schedule-task\ntime = "09:00"\ntimezone = "UTC"\n'
                    'prompt = "Check on the build."\n```'
                ),
                done=True,
            )

    class _SchedulingAgent(_FakeAgent):
        async def start_session(self, session_id, work_dir, platform_system_prompt="", show_footer=False):
            return _SchedulingAgentSession()

    agent = _SchedulingAgent()
    workspace = WorkspaceManager(tmp_path / "workspace")
    router = Router(default_agent="fake", workspace=workspace)
    session_store = SessionStore(tmp_path / "sessions.json")
    engine = Engine(
        agents={"fake": agent},
        routers={"fake": router},
        session_store=session_store,
        commands=CommandRegistry(),
        scheduled_tasks_dir=tmp_path,
    )
    platform = _FakePlatform()

    await engine.on_message(platform, _make_message())

    assert len(platform.replies) == 1
    assert "Sure, I'll check in daily." in platform.replies[0]
    assert "schedule-task" not in platform.replies[0]
    assert "Scheduled daily at 09:00 UTC." in platform.replies[0]

    from pocketagent.core.scheduled_tasks import load_scheduled_tasks

    tasks = load_scheduled_tasks(tmp_path)
    assert len(tasks) == 1
    assert tasks[0].platform == "fake"
    assert tasks[0].channel_id == "1"
    assert tasks[0].user_id == "1"
    assert tasks[0].time == "09:00"
    assert tasks[0].timezone == "UTC"
    assert tasks[0].prompt == "Check on the build."


@pytest.mark.asyncio
async def test_on_message_schedule_task_error_shown_and_nothing_written(tmp_path):
    class _BadSchedulingAgentSession(_FakeAgentSession):
        async def events(self) -> AsyncIterator[Event]:
            yield Event(
                type=EventType.RESULT,
                content='```schedule-task\ntime = "99:99"\nprompt = "hi"\n```',
                done=True,
            )

    class _BadSchedulingAgent(_FakeAgent):
        async def start_session(self, session_id, work_dir, platform_system_prompt="", show_footer=False):
            return _BadSchedulingAgentSession()

    agent = _BadSchedulingAgent()
    workspace = WorkspaceManager(tmp_path / "workspace")
    router = Router(default_agent="fake", workspace=workspace)
    session_store = SessionStore(tmp_path / "sessions.json")
    engine = Engine(
        agents={"fake": agent},
        routers={"fake": router},
        session_store=session_store,
        commands=CommandRegistry(),
        scheduled_tasks_dir=tmp_path,
    )
    platform = _FakePlatform()

    await engine.on_message(platform, _make_message())

    assert "Couldn't schedule that" in platform.replies[0]
    assert not (tmp_path / "scheduled_tasks.toml").exists()


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
                effort="high",
                cost_usd=0.0533424,
                context_used_pct=14,
                rate_limit_5h_pct=84,
                rate_limit_5h_reset_in="2h49m",
                rate_limit_7d_pct=14,
                rate_limit_7d_reset_in="2d",
            )

    class _AgentWithUsage(_FakeAgent):
        async def start_session(
            self, session_id, work_dir, platform_system_prompt="", show_footer=False
        ) -> AgentSession:
            return _AgentSessionWithUsage()

    engine, _ = _make_engine(tmp_path)
    engine.agents["fake"] = _AgentWithUsage()
    platform = _FakePlatform()

    await engine.on_message(platform, _make_message())

    # event.model is already display-formatted by the agent backend that set it
    # (here, a raw test value) -- the engine just passes it through unchanged.
    assert platform.replies == [
        "ok\n\n`claude-sonnet-4-6 · high · 14 tokens · ctx:14% · 5h:84%(2h49m) · 7d:14%(2d) · $0.0533`"
    ]


@pytest.mark.asyncio
async def test_clear_all_sessions_closes_live_sessions_and_forgets_resume_ids(tmp_path):
    engine, _ = _make_engine(tmp_path)
    platform = _FakePlatform()
    msg = _make_message()

    await engine.on_message(platform, msg)
    assert msg.session_key in engine.session_store._live
    engine.session_store.set_resume_id(msg.session_key, "resume-123")

    await engine.clear_all_sessions()

    assert engine.session_store._live == {}
    assert engine.session_store._resume_ids == {}


@pytest.mark.asyncio
async def test_clear_sessions_only_clears_keys_matching_predicate(tmp_path):
    engine, _ = _make_engine(tmp_path)
    platform = _FakePlatform()
    msg_a = _make_message()
    msg_a.session_key = "fake:111:1"
    msg_b = _make_message()
    msg_b.session_key = "fake:222:1"

    await engine.on_message(platform, msg_a)
    await engine.on_message(platform, msg_b)

    await engine.clear_sessions(lambda key: key.startswith("fake:111:"))

    assert "fake:111:1" not in engine.session_store._live
    assert "fake:222:1" in engine.session_store._live


@pytest.mark.asyncio
async def test_error_matching_usage_limit_denial_queues_instead_of_showing_error(tmp_path):
    class _DeniedAgentSession(_FakeAgentSession):
        async def events(self) -> AsyncIterator[Event]:
            # rate_limit_retry_at is set directly here rather than relying on
            # text parsing -- that parsing is claude_code-specific (see
            # claude_code._parse_limit_denied) and this fake "fake" agent
            # backend isn't claude_code, so the engine must consume only the
            # generic, already-computed field.
            yield Event(
                type=EventType.ERROR,
                error="You've hit your session limit · resets 11:59pm (UTC)",
                done=True,
                rate_limit_retry_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )

    class _DeniedAgent(_FakeAgent):
        async def start_session(
            self, session_id, work_dir, platform_system_prompt="", show_footer=False
        ) -> AgentSession:
            return _DeniedAgentSession()

    engine, _ = _make_engine(tmp_path)
    engine.agents["fake"] = _DeniedAgent()
    platform = _FakePlatform()
    msg = _make_message()

    await engine.on_message(platform, msg)

    assert "queued" in platform.replies[-1]
    assert "fake" in engine._rate_limit_timers
    assert engine._rate_limit_backlog["fake"] == [(platform, msg)]

    for timer in engine._rate_limit_timers.values():
        await timer.stop()


@pytest.mark.asyncio
async def test_message_while_agent_rate_limited_queues_without_calling_agent(tmp_path):
    engine, agent = _make_engine(tmp_path)
    platform = _FakePlatform()
    msg = _make_message()

    engine._mark_rate_limited("fake", datetime.now(timezone.utc) + timedelta(hours=1))

    await engine.on_message(platform, msg)

    assert "queued" in platform.replies[-1]
    assert engine._rate_limit_backlog["fake"] == [(platform, msg)]
    assert not hasattr(agent, "last_platform_system_prompt")  # start_session never called

    for timer in engine._rate_limit_timers.values():
        await timer.stop()


@pytest.mark.asyncio
async def test_result_with_rate_limit_pct_100_marks_agent_for_next_message(tmp_path):
    class _MaxedAgentSession(_FakeAgentSession):
        async def events(self) -> AsyncIterator[Event]:
            yield Event(
                type=EventType.RESULT,
                content="ok",
                done=True,
                rate_limit_5h_pct=100,
                rate_limit_5h_reset_in="11m",
            )

    class _MaxedAgent(_FakeAgent):
        async def start_session(
            self, session_id, work_dir, platform_system_prompt="", show_footer=False
        ) -> AgentSession:
            return _MaxedAgentSession()

    engine, _ = _make_engine(tmp_path)
    engine.agents["fake"] = _MaxedAgent()
    platform = _FakePlatform()

    await engine.on_message(platform, _make_message())

    assert "ok" in platform.replies[0]  # this message's own reply still goes through
    assert "fake" in engine._rate_limit_timers

    for timer in engine._rate_limit_timers.values():
        await timer.stop()


@pytest.mark.asyncio
async def test_backlog_flushes_and_replays_queued_messages_when_timer_fires(tmp_path):
    engine, _ = _make_engine(tmp_path)
    platform = _FakePlatform()
    msg = _make_message()

    engine._mark_rate_limited("fake", datetime.now(timezone.utc) - timedelta(seconds=1))
    await engine.on_message(platform, msg)
    assert "queued" in platform.replies[-1]
    assert engine._rate_limit_backlog["fake"] == [(platform, msg)]

    for _ in range(10):
        await asyncio.sleep(0)
        if "fake" not in engine._rate_limit_timers:
            break

    assert "fake" not in engine._rate_limit_timers
    assert engine._rate_limit_backlog.get("fake", []) == []
    assert platform.replies[-1] == "ok"  # replayed message got a normal reply this time


@pytest.mark.asyncio
async def test_on_message_queues_second_message_while_first_is_in_flight(tmp_path):
    """A second message for the same session_key must wait for the first turn
    to finish (and the same AgentSession instance) instead of racing it --
    concurrent sends to one session would corrupt a stream-protocol agent."""

    gate = asyncio.Event()
    order: list[str] = []

    class _GatedAgentSession(_FakeAgentSession):
        async def send(self, prompt, images=(), files=()):
            order.append(f"send:{prompt}")
            self.sent.append(prompt)

        async def events(self) -> AsyncIterator[Event]:
            if self.sent == ["first"]:
                await gate.wait()
            order.append(f"events:{self.sent[-1]}")
            yield Event(type=EventType.RESULT, content=self.sent[-1], done=True)

    class _GatedAgent(_FakeAgent):
        async def start_session(
            self, session_id, work_dir, platform_system_prompt="", show_footer=False
        ) -> AgentSession:
            self.session = getattr(self, "session", None) or _GatedAgentSession()
            return self.session

    engine, _ = _make_engine(tmp_path)
    agent = _GatedAgent()
    engine.agents["fake"] = agent
    platform = _FakePlatform()

    msg1 = _make_message()
    msg1.content = "first"
    msg2 = _make_message()
    msg2.content = "second"

    task1 = asyncio.create_task(engine.on_message(platform, msg1))
    await asyncio.sleep(0)  # let task1 start and block inside events()
    task2 = asyncio.create_task(engine.on_message(platform, msg2))
    await asyncio.sleep(0)  # let task2 observe the lock is held and queue

    assert any("queued" in r for r in platform.replies)
    assert order == ["send:first"]  # second message hasn't sent yet

    gate.set()
    await asyncio.gather(task1, task2)

    assert order == ["send:first", "events:first", "send:second", "events:second"]
    assert platform.replies[-2:] == ["first", "second"]
