import os
import shutil

import pytest

from pocketagent.agents.tmux import (
    TmuxAgent,
    TmuxSession,
    _tmux,
    clean_tui_content,
    extract_new,
    normalize_capture,
    resolve_target,
    unique_window_name,
)
from pocketagent.core.types import FileAttachment, ImageAttachment

TMUX_AVAILABLE = shutil.which("tmux") is not None


# --- extract_new --------------------------------------------------------------


@pytest.mark.parametrize(
    "baseline, current, want",
    [
        ("foo\nbar", "foo\nbar", ""),
        ("", "hello", "hello"),
        ("foo\nbar", "foo\nbar\nbaz", "baz"),
        (
            "user@host:~$ ",
            "user@host:~$ ls\nfile1\nfile2\nuser@host:~$ ",
            "ls\nfile1\nfile2\nuser@host:~$ ",
        ),
        ("line1\nline2\nline3\nline4\nline5", "line3\nline4\nline5\nnew1\nnew2", "new1\nnew2"),
        ("old1\nold2\nold3", "new1\nnew2\nnew3", "new1\nnew2\nnew3"),
        ("╭─ Claude ─╮\n\n>", "╭─ Claude ─╮\n\nThe answer is 42.\n\n>", "The answer is 42."),
        ("header\n\n>", "header\n\nLine one.\nLine two.\n\n>", "Line one.\nLine two."),
        (
            "h1\nh2\nh3\nh4\nh5\nUIa\nUIb",
            "h3\nh4\nh5\nRESP\nUIa\nUIb",
            "RESP",
        ),
        (
            "h1\nh2\nh3\nB1\nB2\nB3\nS_old",
            "h3\nR1\nR2\nB1\nB2\nB3\nS_new",
            "R1\nR2\nB1\nB2\nB3\nS_new",
        ),
    ],
)
def test_extract_new(baseline, current, want):
    assert extract_new(baseline, current) == want


# --- clean_tui_content / normalize_capture -----------------------------------


def test_clean_tui_content_collapses_consecutive_blanks():
    text = " ✳ Thinking...\n\n\n  tokens:22k"
    assert clean_tui_content(text, strip_input_block=False, strip_patterns=[]) == " ✳ Thinking...\n\n  tokens:22k"


def test_normalize_capture_strips_trailing_spaces():
    assert normalize_capture("hello   \nworld   \n") == "hello\nworld"


def test_normalize_capture_strips_ansi_color_codes():
    assert normalize_capture("\x1b[32mgreen\x1b[0m normal") == "green normal"


def test_normalize_capture_strips_osc_sequence():
    assert normalize_capture("\x1b]0;title\x07prompt$ ") == "prompt$"


# --- TmuxAgent validation / target resolution ---------------------------------


def test_agent_requires_session_name():
    with pytest.raises(ValueError):
        TmuxAgent(session="")


@pytest.mark.skipif(not TMUX_AVAILABLE, reason="tmux not installed")
def test_resolve_target_unique_per_work_dir():
    target1, win1 = resolve_target("mywork", "0", "/repo/a/app")
    target2, win2 = resolve_target("mywork", "0", "/repo/b/app")
    assert target1 != target2
    assert win1 != win2


@pytest.mark.skipif(not TMUX_AVAILABLE, reason="tmux not installed")
def test_resolve_target_stable():
    t1, w1 = resolve_target("mywork", "0", "/repo/a/app")
    t2, w2 = resolve_target("mywork", "0", "/repo/a/app")
    assert (t1, w1) == (t2, w2)


def test_resolve_target_falls_back_to_session_pane_for_dot():
    target, window_name = resolve_target("mywork", "0", ".")
    assert target == "mywork:0"
    assert window_name == "0"


def test_unique_window_name_collision_safe_for_shared_basename():
    assert unique_window_name("/repo/a/app") != unique_window_name("/repo/b/app")


# --- TmuxSession (constructed directly, no real tmux pane needed) ------------


def test_tmux_session_stores_work_dir():
    session = TmuxSession(
        target="sess:win",
        session_id="sid1",
        work_dir="/tmp/workspace",
        prompt_pattern="",
        poll_interval=0.2,
        strip_input_block=False,
        strip_patterns=[],
    )
    assert session._work_dir == "/tmp/workspace"
    assert session.current_session_id == "sid1"
    assert session.alive()


def test_send_images_promoted_to_files(tmp_path):
    from pocketagent.core.attachments import save_files

    images = [ImageAttachment(mime_type="image/png", data=b"\x89PNG\r\n", file_name="screenshot.png")]
    files = [FileAttachment(mime_type="text/plain", data=b"hello", file_name="note.txt")]

    all_files = list(files) + [
        FileAttachment(mime_type=img.mime_type, data=img.data, file_name=img.file_name) for img in images
    ]
    paths = save_files(str(tmp_path), all_files)

    assert len(paths) == 2
    assert any(p.endswith("screenshot.png") for p in paths)


# --- end-to-end against a real tmux pane --------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not TMUX_AVAILABLE, reason="tmux not installed")
async def test_full_turn_against_real_tmux_pane(tmp_path):
    session_name = f"pocketagent-test-{os.getpid()}"
    agent = TmuxAgent(session=session_name, poll_interval_ms=100)
    try:
        agent_session = await agent.start_session(None, str(tmp_path))
        try:
            await agent_session.send("echo hello-pocketagent")
            events = [event async for event in agent_session.events()]
            result = next(e for e in events if e.done)
            assert "hello-pocketagent" in result.content
        finally:
            await agent_session.close()
    finally:
        await _tmux("kill-session", "-t", session_name)
