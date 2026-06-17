import asyncio
import json
import sys
from pathlib import Path

import pytest

from pocketagent.agents.claude_code import (
    ClaudeCodeAgent,
    ClaudeCodeSession,
    _build_user_message,
    _save_files,
    translate_message,
)
from pocketagent.core.types import EventType, FileAttachment, ImageAttachment


# --- translate_message (pure parsing) --------------------------------------


def test_translate_system_message():
    events = translate_message({"type": "system", "subtype": "init", "session_id": "s1"})
    assert len(events) == 1
    assert events[0].type == EventType.THINKING
    assert events[0].session_id == "s1"


def test_translate_assistant_text():
    msg = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Hello!"}]},
        "session_id": "s1",
    }
    events = translate_message(msg)
    assert len(events) == 1
    assert events[0].type == EventType.TEXT
    assert events[0].content == "Hello!"


def test_translate_assistant_tool_use():
    msg = {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]
        },
        "session_id": "s1",
    }
    events = translate_message(msg)
    assert len(events) == 1
    assert events[0].type == EventType.TOOL_USE
    assert events[0].tool_name == "Bash"
    assert json.loads(events[0].tool_input) == {"command": "ls"}


def test_translate_assistant_multiple_blocks():
    msg = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "let me think"},
                {"type": "text", "text": "done"},
            ]
        },
    }
    events = translate_message(msg)
    assert [e.type for e in events] == [EventType.THINKING, EventType.TEXT]


def test_translate_control_request():
    msg = {
        "type": "control_request",
        "request_id": "req-1",
        "request": {"tool_name": "Bash", "input": {"command": "ls"}},
        "session_id": "s1",
    }
    events = translate_message(msg)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == EventType.PERMISSION_REQUEST
    assert ev.request_id == "req-1"
    assert ev.tool_name == "Bash"


def test_translate_result_success():
    msg = {
        "type": "result",
        "subtype": "success",
        "result": "All done",
        "session_id": "s1",
        "usage": {"input_tokens": 10, "output_tokens": 4},
    }
    events = translate_message(msg)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == EventType.RESULT
    assert ev.done is True
    assert ev.content == "All done"
    assert ev.input_tokens == 10
    assert ev.output_tokens == 4


def test_translate_result_error():
    msg = {"type": "result", "is_error": True, "result": "boom", "session_id": "s1"}
    events = translate_message(msg)
    assert events[0].type == EventType.ERROR
    assert events[0].done is True
    assert events[0].error == "boom"


def test_translate_unknown_type_returns_empty():
    assert translate_message({"type": "something_else"}) == []


# --- _build_user_message / _save_files --------------------------------------


def test_build_user_message_text_only():
    msg = _build_user_message("hello", [], [])
    assert msg == {"type": "user", "message": {"role": "user", "content": "hello"}}


def test_build_user_message_with_file_refs():
    msg = _build_user_message("check this", [], ["/tmp/a.txt"])
    content = msg["message"]["content"]
    assert "check this" in content
    assert "/tmp/a.txt" in content


def test_build_user_message_with_image():
    img = ImageAttachment(mime_type="image/png", data=b"\x89PNG...", file_name="a.png")
    msg = _build_user_message("look", [img], [])
    blocks = msg["message"]["content"]
    assert isinstance(blocks, list)
    assert blocks[0] == {"type": "text", "text": "look"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["media_type"] == "image/png"


def test_save_files_writes_to_attachments_dir(tmp_path):
    files = [FileAttachment(mime_type="text/plain", data=b"hi", file_name="note.txt")]
    paths = _save_files(str(tmp_path), files)
    assert len(paths) == 1
    saved = Path(paths[0])
    assert saved.read_bytes() == b"hi"
    assert saved.parent == tmp_path / ".pocketagent" / "attachments"


def test_save_files_sanitizes_path_traversal(tmp_path):
    files = [FileAttachment(mime_type="text/plain", data=b"hi", file_name="../../escape.txt")]
    paths = _save_files(str(tmp_path), files)
    saved = Path(paths[0])
    assert saved.parent == tmp_path / ".pocketagent" / "attachments"
    assert saved.name == "escape.txt"


# --- ClaudeCodeSession end-to-end against a fake subprocess -----------------

FAKE_CLAUDE_BASIC = """
import sys, json
sys.stdin.readline()
lines = [
    {"type": "system", "subtype": "init", "session_id": "sess-1"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello!"}]}, "session_id": "sess-1"},
    {"type": "result", "subtype": "success", "result": "Hello!", "session_id": "sess-1",
     "usage": {"input_tokens": 5, "output_tokens": 2}},
]
for line in lines:
    print(json.dumps(line), flush=True)
"""

FAKE_CLAUDE_PERMISSION = """
import sys, json
sys.stdin.readline()
print(json.dumps({"type": "control_request", "request_id": "req-1",
                   "request": {"tool_name": "Bash", "input": {"command": "ls"}},
                   "session_id": "sess-1"}), flush=True)
resp = json.loads(sys.stdin.readline())
behavior = resp["response"]["response"]["behavior"]
print(json.dumps({"type": "result", "subtype": "success",
                   "result": "approved" if behavior == "allow" else "denied",
                   "session_id": "sess-1", "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
"""


async def _spawn_fake(tmp_path: Path, script: str) -> asyncio.subprocess.Process:
    script_path = tmp_path / "fake_claude.py"
    script_path.write_text(script)
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(script_path),
        cwd=str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


@pytest.mark.asyncio
async def test_session_basic_turn(tmp_path):
    process = await _spawn_fake(tmp_path, FAKE_CLAUDE_BASIC)
    session = ClaudeCodeSession(process, str(tmp_path))
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
        assert session.current_session_id == "sess-1"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_auto_approves_permission_request(tmp_path):
    process = await _spawn_fake(tmp_path, FAKE_CLAUDE_PERMISSION)
    session = ClaudeCodeSession(process, str(tmp_path))
    try:
        await session.send("do something")
        events = []
        async for ev in session.events():
            events.append(ev)
        assert any(e.type == EventType.PERMISSION_REQUEST for e in events)
        result_events = [e for e in events if e.done]
        assert result_events[0].content == "approved"
    finally:
        await session.close()


# --- ClaudeCodeAgent.start_session builds the right CLI invocation ----------


class _FakeStdin:
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass


class _FakeStdout:
    async def readline(self) -> bytes:
        return b""  # immediate EOF, so the reader task exits cleanly


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout()

    def terminate(self) -> None:
        self.returncode = 0

    async def wait(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_start_session_passes_print_and_verbose(monkeypatch):
    # --print is required for claude to run non-interactively at all (without
    # it, claude starts its interactive TUI, which immediately exits with no
    # TTY attached -- the bug this test guards against); --verbose is
    # required by --output-format=stream-json when combined with --print.
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(command, *args, **kwargs):
        captured["command"] = command
        captured["args"] = list(args)
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    agent = ClaudeCodeAgent()
    session = await agent.start_session(None, "/tmp")
    try:
        assert "--print" in captured["args"]
        assert "--verbose" in captured["args"]
    finally:
        await session.close()
