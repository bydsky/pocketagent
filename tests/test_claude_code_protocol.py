import asyncio
import json
import sys
from pathlib import Path

import pytest

from datetime import datetime, timezone

from pocketagent.agents.claude_code import (
    ClaudeCodeAgent,
    ClaudeCodeSession,
    _build_user_message,
    _compute_context_used_pct,
    _format_duration,
    _format_model_name,
    _parse_reset_in,
    _parse_usage_text,
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
        "total_cost_usd": 0.0533424,
    }
    events = translate_message(msg)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == EventType.RESULT
    assert ev.done is True
    assert ev.content == "All done"
    assert ev.input_tokens == 10
    assert ev.output_tokens == 4
    assert ev.cost_usd == 0.0533424


def test_translate_result_error():
    msg = {"type": "result", "is_error": True, "result": "boom", "session_id": "s1"}
    events = translate_message(msg)
    assert events[0].type == EventType.ERROR
    assert events[0].done is True
    assert events[0].error == "boom"


def test_translate_unknown_type_returns_empty():
    assert translate_message({"type": "something_else"}) == []


def test_compute_context_used_pct_matches_real_statusline_payload():
    # Real values from a captured statusLine hook payload: 3 + 71048 + 12454 = 83505
    # input-side tokens over a 600000 context window -> CLI reported used_percentage 14.
    msg = {
        "usage": {
            "input_tokens": 3,
            "cache_creation_input_tokens": 71048,
            "cache_read_input_tokens": 12454,
        },
        "modelUsage": {"claude-sonnet-4-6": {"contextWindow": 600000}},
    }
    assert _compute_context_used_pct(msg, "claude-sonnet-4-6") == 14


def test_compute_context_used_pct_returns_none_without_model_usage():
    assert _compute_context_used_pct({"usage": {"input_tokens": 1}}, "claude-sonnet-4-6") is None


def test_parse_usage_text_extracts_both_percentages_and_resets():
    text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "Current session: 40% used · resets Jun 19, 2:29pm (Australia/Sydney)\n"
        "Current week (all models): 17% used · resets Jun 23, 5:59pm (Australia/Sydney)\n"
    )
    # 2026-06-19 04:18 UTC == 2026-06-19 14:18 Sydney (UTC+10), 11 minutes before the
    # 5h reset (Jun 19, 2:29pm) and just over 4 days before the 7d reset (Jun 23, 5:59pm).
    now = datetime(2026, 6, 19, 4, 18, tzinfo=timezone.utc)
    assert _parse_usage_text(text, now) == (40, "11m", 17, "4d")


def test_parse_usage_text_returns_none_when_unrecognized():
    assert _parse_usage_text("not a usage report") == (None, None, None, None)


def test_parse_usage_text_omits_reset_when_clause_missing():
    text = "Current session: 40% used\nCurrent week (all models): 17% used\n"
    assert _parse_usage_text(text) == (40, None, 17, None)


def test_parse_reset_in_handles_year_rollover():
    # Dec 31 -> a reset stamped "Jan 2" with no year must roll to next year,
    # not be treated as already-past.
    now = datetime(2026, 12, 31, 12, 0, tzinfo=timezone.utc)
    assert _parse_reset_in("Jan 2, 12:00pm", "UTC", now) == "2d"


def test_parse_reset_in_returns_none_for_unknown_timezone():
    now = datetime(2026, 6, 19, 4, 40, tzinfo=timezone.utc)
    assert _parse_reset_in("Jun 19, 2:29pm", "Not/AZone", now) is None


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (5, "5m"),
        (169, "2h49m"),
        (120, "2h"),
        (60 * 24 * 2, "2d"),
        (60 * 24 * 2 + 60, "2d"),  # days drop any remaining hours, matching the footer's compact form
    ],
)
def test_format_duration(minutes, expected):
    from datetime import timedelta

    assert _format_duration(timedelta(minutes=minutes)) == expected


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("claude-sonnet-4-6", "Sonnet 4.6"),
        ("claude-opus-4-8", "Opus 4.8"),
        ("claude-haiku-4-5-20251001", "Haiku 4.5"),
        ("gpt-5-codex", "gpt-5-codex"),
        ("", ""),
    ],
)
def test_format_model_name(model_id, expected):
    assert _format_model_name(model_id) == expected


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
    {"type": "assistant", "message": {"model": "claude-sonnet-4-6", "content": [{"type": "text", "text": "Hello!"}]}, "session_id": "sess-1"},
    {"type": "result", "subtype": "success", "result": "Hello!", "session_id": "sess-1",
     "usage": {"input_tokens": 5, "output_tokens": 2, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 50},
     "total_cost_usd": 0.01,
     "modelUsage": {"claude-sonnet-4-6": {"contextWindow": 1000}}},
]
for line in lines:
    print(json.dumps(line), flush=True)
"""

FAKE_CLAUDE_WITH_USAGE = """
import sys, json
sys.stdin.readline()
lines = [
    {"type": "assistant", "message": {"model": "claude-sonnet-4-6", "content": [{"type": "text", "text": "Hello!"}]}, "session_id": "sess-1"},
    {"type": "result", "subtype": "success", "result": "Hello!", "session_id": "sess-1",
     "usage": {"input_tokens": 5, "output_tokens": 2}},
]
for line in lines:
    print(json.dumps(line), flush=True)
sys.stdin.readline()  # the /usage turn
print(json.dumps({"type": "result", "subtype": "success",
                   "result": "Current session: 40% used · resets Dec 31, 11:59pm (UTC)\\n"
                             "Current week (all models): 17% used · resets Dec 31, 11:58pm (UTC)"}), flush=True)
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
        assert result_events[0].model == "Sonnet 4.6"
        assert result_events[0].cost_usd == 0.01
        assert result_events[0].context_used_pct == 16
        assert session.current_session_id == "sess-1"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_fetches_rate_limits_via_usage_command(tmp_path):
    process = await _spawn_fake(tmp_path, FAKE_CLAUDE_WITH_USAGE)
    session = ClaudeCodeSession(process, str(tmp_path), show_footer=True)
    try:
        await session.send("hi")
        events = []
        async for ev in session.events():
            events.append(ev)
        result_events = [e for e in events if e.done]
        assert result_events[0].rate_limit_5h_pct == 40
        assert result_events[0].rate_limit_7d_pct == 17
        # exact countdown depends on real "now" vs. the fixture's fixed Dec 31
        # reset stamps; just check each landed in the right unit.
        assert result_events[0].rate_limit_5h_reset_in.endswith(("m", "h", "d"))
        assert result_events[0].rate_limit_7d_reset_in.endswith(("m", "h", "d"))
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_skips_usage_turn_when_show_footer_is_false(tmp_path):
    process = await _spawn_fake(tmp_path, FAKE_CLAUDE_WITH_USAGE)
    session = ClaudeCodeSession(process, str(tmp_path))  # show_footer defaults False
    try:
        await session.send("hi")
        events = []
        async for ev in session.events():
            events.append(ev)
        result_events = [e for e in events if e.done]
        # the channel isn't configured to show the footer, so the extra
        # /usage turn (and its cost/transcript noise) must be skipped.
        assert result_events[0].rate_limit_5h_pct is None
        assert result_events[0].rate_limit_7d_pct is None
        assert result_events[0].rate_limit_5h_reset_in is None
        assert result_events[0].rate_limit_7d_reset_in is None
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


@pytest.mark.asyncio
async def test_start_session_appends_platform_system_prompt_when_given(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(command, *args, **kwargs):
        captured["args"] = list(args)
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    agent = ClaudeCodeAgent()
    session = await agent.start_session(None, "/tmp", platform_system_prompt="Be concise.")
    try:
        args = captured["args"]
        assert "--append-system-prompt" in args
        assert args[args.index("--append-system-prompt") + 1] == "Be concise."
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_start_session_appends_agent_system_prompt_when_given(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(command, *args, **kwargs):
        captured["args"] = list(args)
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    agent = ClaudeCodeAgent(agent_system_prompt="Prefer small diffs.")
    session = await agent.start_session(None, "/tmp")
    try:
        args = captured["args"]
        assert "--append-system-prompt" in args
        assert args[args.index("--append-system-prompt") + 1] == "Prefer small diffs."
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_start_session_combines_agent_and_platform_system_prompts(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(command, *args, **kwargs):
        captured["args"] = list(args)
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    agent = ClaudeCodeAgent(agent_system_prompt="Prefer small diffs.")
    session = await agent.start_session(None, "/tmp", platform_system_prompt="Be concise.")
    try:
        args = captured["args"]
        combined = args[args.index("--append-system-prompt") + 1]
        assert combined == "Prefer small diffs.\n\nBe concise."
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_start_session_omits_system_prompt_flag_when_not_given(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(command, *args, **kwargs):
        captured["args"] = list(args)
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    agent = ClaudeCodeAgent()
    session = await agent.start_session(None, "/tmp")
    try:
        assert "--append-system-prompt" not in captured["args"]
    finally:
        await session.close()
