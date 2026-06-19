import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from pocketagent.agents.codex import (
    CodexAgent,
    CodexSession,
    _toml_quote,
    build_exec_args,
    translate_message,
)
from pocketagent.core.types import EventType


# --- translate_message (pure parsing) --------------------------------------


def test_translate_thread_started():
    events = translate_message({"type": "thread.started", "thread_id": "th-1"})
    assert len(events) == 1
    assert events[0].type == EventType.THINKING
    assert events[0].session_id == "th-1"


def test_translate_agent_message():
    msg = {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "Hello!"}}
    events = translate_message(msg)
    assert len(events) == 1
    assert events[0].type == EventType.TEXT
    assert events[0].content == "Hello!"


def test_translate_reasoning_item():
    msg = {"type": "item.completed", "item": {"type": "reasoning", "text": "thinking..."}}
    events = translate_message(msg)
    assert events[0].type == EventType.THINKING
    assert events[0].content == "thinking..."


def test_translate_command_execution_item_as_tool_use():
    msg = {
        "type": "item.completed",
        "item": {"id": "i2", "type": "command_execution", "command": "ls", "exit_code": 0},
    }
    events = translate_message(msg)
    assert len(events) == 1
    assert events[0].type == EventType.TOOL_USE
    assert events[0].tool_name == "command_execution"
    assert json.loads(events[0].tool_input)["command"] == "ls"


def test_translate_unknown_item_type_returns_empty():
    msg = {"type": "item.completed", "item": {"type": "something_new"}}
    assert translate_message(msg) == []


def test_translate_turn_completed():
    msg = {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 4}}
    events = translate_message(msg)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == EventType.RESULT
    assert ev.done is True
    assert ev.input_tokens == 10
    assert ev.output_tokens == 4


def test_translate_turn_failed():
    msg = {"type": "turn.failed", "error": {"message": "boom"}}
    events = translate_message(msg)
    assert events[0].type == EventType.ERROR
    assert events[0].done is True
    assert events[0].error == "boom"


def test_translate_error_event():
    msg = {"type": "error", "message": "bad request"}
    events = translate_message(msg)
    assert events[0].type == EventType.ERROR
    assert events[0].done is True
    assert events[0].error == "bad request"


def test_translate_unknown_type_returns_empty():
    assert translate_message({"type": "something_else"}) == []


# --- _toml_quote --------------------------------------------------------------


def test_toml_quote_escapes_quotes_and_newlines():
    assert _toml_quote('say "hi"\nbye') == '"say \\"hi\\"\\nbye"'


# --- build_exec_args -----------------------------------------------------------


def test_build_exec_args_minimal():
    args = build_exec_args(
        session_id=None,
        sandbox="",
        ask_for_approval="",
        model="",
        system_prompt="",
        image_paths=[],
        extra_args=[],
        skip_git_repo_check=False,
        prompt="hello",
    )
    assert args == ["exec", "--json", "hello"]


def test_build_exec_args_full():
    args = build_exec_args(
        session_id="th-1",
        sandbox="workspace-write",
        ask_for_approval="never",
        model="gpt-5.4",
        system_prompt="Be concise.",
        image_paths=["/tmp/a.png"],
        extra_args=["--foo"],
        skip_git_repo_check=True,
        prompt="hello",
    )
    assert args == [
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "--model",
        "gpt-5.4",
        "-c",
        'developer_instructions="Be concise."',
        "--image",
        "/tmp/a.png",
        "--foo",
        "resume",
        "th-1",
        "hello",
    ]


# --- CodexSession end-to-end against a fake subprocess ----------------------

FAKE_CODEX_BASIC = """#!/usr/bin/env python3
import sys, json
lines = [
    {"type": "thread.started", "thread_id": "th-1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "Hello!"}},
    {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
]
for line in lines:
    print(json.dumps(line), flush=True)
"""

FAKE_CODEX_RESUME_ECHO = """#!/usr/bin/env python3
import sys, json
args = sys.argv[1:]
resumed = "resume" in args
print(json.dumps({"type": "thread.started", "thread_id": "th-1"}), flush=True)
print(json.dumps({"type": "item.completed",
                   "item": {"type": "agent_message", "text": "resumed" if resumed else "fresh"}}),
      flush=True)
print(json.dumps({"type": "turn.completed", "usage": {}}), flush=True)
"""

FAKE_CODEX_CRASH = """#!/usr/bin/env python3
import sys, json
print(json.dumps({"type": "thread.started", "thread_id": "th-1"}), flush=True)
sys.exit(1)
"""


async def _spawn_fake(tmp_path: Path, script: str, name: str = "fake_codex.py") -> CodexSession:
    script_path = tmp_path / name
    script_path.write_text(script)
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)
    return CodexSession(
        command=str(script_path),
        work_dir=str(tmp_path),
        session_id=None,
        sandbox="",
        ask_for_approval="",
        model="",
        system_prompt="",
        extra_args=[],
        skip_git_repo_check=False,
    )


@pytest.mark.asyncio
async def test_session_basic_turn(tmp_path):
    session = await _spawn_fake(tmp_path, FAKE_CODEX_BASIC)
    try:
        await session.send("hi")
        events = []
        async for ev in session.events():
            events.append(ev)
        assert any(e.type == EventType.TEXT and e.content == "Hello!" for e in events)
        result_events = [e for e in events if e.done]
        assert len(result_events) == 1
        assert result_events[0].type == EventType.RESULT
        assert result_events[0].input_tokens == 5
        assert session.current_session_id == "th-1"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_resumes_with_captured_thread_id(tmp_path):
    session = await _spawn_fake(tmp_path, FAKE_CODEX_RESUME_ECHO)
    try:
        await session.send("turn 1")
        events = [ev async for ev in session.events()]
        assert any(e.content == "fresh" for e in events)

        await session.send("turn 2")
        events = [ev async for ev in session.events()]
        assert any(e.content == "resumed" for e in events)
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_emits_error_on_unexpected_exit(tmp_path):
    session = await _spawn_fake(tmp_path, FAKE_CODEX_CRASH)
    try:
        await session.send("hi")
        events = [ev async for ev in session.events()]
        result_events = [e for e in events if e.done]
        assert len(result_events) == 1
        assert result_events[0].type == EventType.ERROR
    finally:
        await session.close()


# --- CodexAgent.start_session ------------------------------------------------


@pytest.mark.asyncio
async def test_start_session_does_not_spawn_a_process(monkeypatch):
    called = False

    async def fake_create_subprocess_exec(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    agent = CodexAgent()
    session = await agent.start_session(None, "/tmp")
    assert not called
    assert isinstance(session, CodexSession)
    assert session.alive()


@pytest.mark.asyncio
async def test_start_session_combines_agent_and_platform_system_prompts(monkeypatch):
    agent = CodexAgent(agent_system_prompt="Prefer small diffs.")
    session = await agent.start_session(None, "/tmp", platform_system_prompt="Be concise.")
    assert session._system_prompt == "Prefer small diffs.\n\nBe concise."
