from dataclasses import replace
from typing import AsyncIterator

import pytest

from pocketagent.core.agent import Agent, AgentSession
from pocketagent.core.commands import CommandRegistry
from pocketagent.core.engine import Engine
from pocketagent.core.platform import Platform
from pocketagent.core.router import Router
from pocketagent.core.scheduled_tasks import (
    ScheduledTask,
    append_scheduled_task,
    load_scheduled_tasks,
    run_scheduled_task,
)
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


def test_append_scheduled_task_creates_file_and_is_readable_back(tmp_path):
    task = ScheduledTask(
        platform="discord", channel_id="111", user_id="222", cron="0 9 * * *", prompt="hi", timezone="UTC"
    )

    task_id = append_scheduled_task(tmp_path, task)

    loaded = load_scheduled_tasks(tmp_path)
    assert loaded == [replace(task, id=task_id)]


def test_append_scheduled_task_assigns_and_persists_an_id_when_missing(tmp_path):
    task = ScheduledTask(platform="discord", channel_id="1", user_id="1", cron="0 9 * * *", prompt="hi")
    assert task.id == ""

    task_id = append_scheduled_task(tmp_path, task)

    assert task_id
    assert load_scheduled_tasks(tmp_path)[0].id == task_id
    # A second load reuses the persisted id rather than generating a new one.
    assert load_scheduled_tasks(tmp_path)[0].id == task_id


def test_append_scheduled_task_keeps_explicit_id(tmp_path):
    task = ScheduledTask(
        platform="discord", channel_id="1", user_id="1", cron="0 9 * * *", prompt="hi", id="my-custom-id"
    )

    task_id = append_scheduled_task(tmp_path, task)

    assert task_id == "my-custom-id"
    assert load_scheduled_tasks(tmp_path)[0].id == "my-custom-id"


def test_append_scheduled_task_twice_keeps_both_entries(tmp_path):
    first = ScheduledTask(platform="discord", channel_id="1", user_id="1", cron="0 9 * * *", prompt="one")
    second = ScheduledTask(platform="discord", channel_id="2", user_id="2", cron="0 10 * * *", prompt="two")

    first_id = append_scheduled_task(tmp_path, first)
    second_id = append_scheduled_task(tmp_path, second)

    assert load_scheduled_tasks(tmp_path) == [replace(first, id=first_id), replace(second, id=second_id)]


def test_append_scheduled_task_escapes_special_characters_in_prompt(tmp_path):
    task = ScheduledTask(
        platform="discord",
        channel_id="1",
        user_id="1",
        cron="0 9 * * *",
        prompt='has "quotes", a\nnewline, and a \\backslash',
    )

    task_id = append_scheduled_task(tmp_path, task)

    assert load_scheduled_tasks(tmp_path) == [replace(task, id=task_id)]


def test_append_scheduled_task_preserves_existing_file_content(tmp_path):
    path = tmp_path / "scheduled_tasks.toml"
    path.write_text("# a hand-written comment\n")
    task = ScheduledTask(platform="discord", channel_id="1", user_id="1", cron="0 9 * * *", prompt="hi")

    task_id = append_scheduled_task(tmp_path, task)

    assert path.read_text().startswith("# a hand-written comment\n")
    assert load_scheduled_tasks(tmp_path) == [replace(task, id=task_id)]


def test_append_and_load_biweekly_task(tmp_path):
    task = ScheduledTask(
        platform="discord",
        channel_id="1",
        user_id="1",
        prompt="check in",
        cron="0 19 * * 4",
        interval_weeks=2,
    )

    task_id = append_scheduled_task(tmp_path, task)

    contents = (tmp_path / "scheduled_tasks.toml").read_text()
    assert "interval_weeks = 2" in contents
    assert load_scheduled_tasks(tmp_path) == [replace(task, id=task_id)]


def test_load_scheduled_tasks_generates_id_when_missing(tmp_path):
    path = tmp_path / "scheduled_tasks.toml"
    path.write_text(
        """
        [[scheduled_tasks]]
        platform = "discord"
        channel_id = "1"
        user_id = "1"
        prompt = "hi"
        cron = "0 9 * * *"
        """
    )
    tasks = load_scheduled_tasks(tmp_path)
    assert tasks[0].id != ""


def test_load_scheduled_tasks_keeps_explicit_id(tmp_path):
    path = tmp_path / "scheduled_tasks.toml"
    path.write_text(
        """
        [[scheduled_tasks]]
        id = "my-task"
        platform = "discord"
        channel_id = "1"
        user_id = "1"
        prompt = "hi"
        cron = "0 9 * * *"
        """
    )
    tasks = load_scheduled_tasks(tmp_path)
    assert tasks[0].id == "my-task"


def test_load_scheduled_tasks_requires_cron(tmp_path):
    path = tmp_path / "scheduled_tasks.toml"
    path.write_text(
        """
        [[scheduled_tasks]]
        platform = "discord"
        channel_id = "1"
        user_id = "1"
        prompt = "hi"
        """
    )
    with pytest.raises(ValueError):
        load_scheduled_tasks(tmp_path)


def test_load_scheduled_tasks_rejects_invalid_cron(tmp_path):
    path = tmp_path / "scheduled_tasks.toml"
    path.write_text(
        """
        [[scheduled_tasks]]
        platform = "discord"
        channel_id = "1"
        user_id = "1"
        prompt = "hi"
        cron = "not a cron expression"
        """
    )
    with pytest.raises(ValueError):
        load_scheduled_tasks(tmp_path)


def test_load_scheduled_tasks_rejects_invalid_interval_weeks(tmp_path):
    path = tmp_path / "scheduled_tasks.toml"
    path.write_text(
        """
        [[scheduled_tasks]]
        platform = "discord"
        channel_id = "1"
        user_id = "1"
        prompt = "hi"
        cron = "0 19 * * 4"
        interval_weeks = 0
        """
    )
    with pytest.raises(ValueError):
        load_scheduled_tasks(tmp_path)
