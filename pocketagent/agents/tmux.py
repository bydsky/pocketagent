"""tmux agent backend.

Drives a persistent tmux pane as an interactive shell agent: a user message
is sent to the pane as literal keystrokes (followed by Enter); the reply is
captured by polling `tmux capture-pane` until the pane goes stable -- and
(if it matches `prompt_pattern`) idle on a prompt -- then diffing the
capture against a baseline snapshot taken right before the keystrokes were
sent.

Typical use: point `init_command` at `claude` (or any other interactive CLI
agent) so the pane runs a TUI that pocketagent drives via tmux send-keys,
rather than speaking a structured protocol the way ClaudeCodeAgent does.

Each distinct work_dir gets its own tmux window (named after the directory,
hash-suffixed so two dirs sharing a basename never collide) inside the
configured session; "." (the default workspace) falls back to the
configured session:pane target.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import AsyncIterator, Sequence

from ..core.agent import Agent, AgentSession
from ..core.attachments import save_files
from ..core.types import Event, EventType, FileAttachment, ImageAttachment

# Matches common shell prompts and Claude Code's ❯ prompt.
DEFAULT_PROMPT_PATTERN = r"[❯\$#>%]\s*$"

# Claude Code's mode status line, stripped from captured output by default.
DEFAULT_STRIP_PATTERNS = [r"⏵⏵.*\(shift\+tab to cycle\)"]

_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*\x07"  # OSC: ESC ] ... BEL
    r"|\x1b\[[0-9;]*[a-zA-Z]"  # CSI: ESC [ params letter
    r"|\x1b."  # other two-char escape sequences
)
# Claude Code's 3-line input area: a ─ separator, a ❯ prompt line, a ─ separator.
_TUI_INPUT_BLOCK_RE = re.compile(r"^─+\n❯[^\n]*\n─+", re.MULTILINE)
_CONSECUTIVE_BLANK_RE = re.compile(r"\n{3,}")
_UNSAFE_WINDOW_CHARS_RE = re.compile(r"[:.\s]")


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _fnv1a_32(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def sanitize_window_name(name: str) -> str:
    """Make a string safe to use as a tmux window name."""

    name = _UNSAFE_WINDOW_CHARS_RE.sub("-", name)
    return name or "default"


def unique_window_name(work_dir: str) -> str:
    """Build a tmux window name unique to work_dir's full path.

    A 4-hex-char FNV-1a hash of the full path is appended to the directory's
    basename so that two work_dirs sharing a basename (e.g. /a/app and
    /b/app) never collide into the same tmux window.
    """

    base = sanitize_window_name(Path(work_dir).name)
    h = _fnv1a_32(work_dir.encode("utf-8")) & 0xFFFF
    return f"{base}-{h:04x}"


def resolve_target(session_name: str, pane: str, work_dir: str) -> tuple[str, str]:
    """Return (tmux_target, window_name) for work_dir."""

    if work_dir and work_dir != ".":
        window_name = unique_window_name(work_dir)
        return f"{session_name}:{window_name}", window_name
    return f"{session_name}:{pane}", pane


# -- tmux subprocess helpers ---------------------------------------------------


async def _tmux(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode or 0, (stdout or b"").decode("utf-8", errors="replace")


async def tmux_session_exists(name: str) -> bool:
    code, _ = await _tmux("has-session", "-t", name)
    return code == 0


async def tmux_window_exists(target: str) -> bool:
    code, _ = await _tmux("has-session", "-t", target)
    return code == 0


async def create_tmux_session(name: str, window_name: str, work_dir: str, shell: str = "") -> None:
    args = ["new-session", "-d", "-s", name, "-n", window_name]
    if work_dir and work_dir != ".":
        args += ["-c", work_dir]
    if shell:
        args.append(shell)
    code, out = await _tmux(*args)
    if code != 0:
        raise RuntimeError(f"tmux: create session {name!r}: {out.strip()}")
    # Enable focus events so Claude Code doesn't warn about them being off.
    await _tmux("set-option", "-t", name, "-g", "focus-events", "on")


async def create_tmux_window(session: str, window_name: str, work_dir: str) -> None:
    # Trailing colon on the target lets tmux pick the next free index, avoiding
    # index collisions when multiple windows are created concurrently.
    args = ["new-window", "-d", "-t", f"{session}:", "-n", window_name]
    if work_dir and work_dir != ".":
        args += ["-c", work_dir]
    code, out = await _tmux(*args)
    if code != 0:
        raise RuntimeError(f"tmux: create window {window_name!r} in session {session!r}: {out.strip()}")


async def capture_pane(target: str) -> str:
    code, out = await _tmux("capture-pane", "-t", target, "-p")
    if code != 0:
        raise RuntimeError(f"tmux: capture-pane {target!r}: {out.strip()}")
    return normalize_capture(out)


async def capture_scrollback(target: str) -> str:
    # "-S -" (start of history) instead of a fixed line count avoids dropping
    # the first lines of a long response that pushed the capture window past
    # the point where the response started.
    code, out = await _tmux("capture-pane", "-t", target, "-p", "-S", "-")
    if code != 0:
        raise RuntimeError(f"tmux: capture-pane (scrollback) {target!r}: {out.strip()}")
    return normalize_capture(out)


async def send_keys(target: str, keys: str) -> None:
    # -l (literal) prevents tmux from interpreting key names (C-c, Enter, Up, ...)
    # embedded in the text. Enter is sent as a separate keystroke afterwards.
    code, out = await _tmux("send-keys", "-t", target, "-l", keys)
    if code != 0:
        raise RuntimeError(f"tmux: send-keys {target!r}: {out.strip()}")
    code, out = await _tmux("send-keys", "-t", target, "Enter")
    if code != 0:
        raise RuntimeError(f"tmux: send-keys Enter {target!r}: {out.strip()}")


def normalize_capture(raw: str) -> str:
    """Trim trailing whitespace per line and strip ANSI/VT escape sequences."""

    raw = _ANSI_RE.sub("", raw)
    lines = [line.rstrip(" \t\r") for line in raw.split("\n")]
    return "\n".join(lines).rstrip("\n")


def clean_tui_content(text: str, strip_input_block: bool, strip_patterns: Sequence[re.Pattern[str]]) -> str:
    """Remove TUI frame lines (input box, status line) from captured output."""

    if strip_input_block:
        text = _TUI_INPUT_BLOCK_RE.sub("", text)
    if strip_patterns:
        lines = text.split("\n")
        text = "\n".join(line for line in lines if not any(p.search(line) for p in strip_patterns))
    text = _CONSECUTIVE_BLANK_RE.sub("\n\n", text)
    return text.rstrip("\n")


def extract_new(baseline: str, current: str) -> str:
    """Diff current against baseline and return only what the agent added.

    Handles three rendering modes:
      - Append mode (content only grows): fast-path prefix strip.
      - TUI redraw / scroll (shared history run found at some offset into
        baseline): return what follows that run, with a redrawn trailing
        input box/prompt trimmed off.
      - No shared history at all (buffer fully scrolled): return at most the
        last `visible_fallback_lines` lines so stale scrollback is never
        mistaken for "the response".
    """

    if current == baseline:
        return ""
    if baseline == "":
        return current

    if current.startswith(baseline):
        return current[len(baseline) :].lstrip("\n")

    base_lines = baseline.split("\n")
    cur_lines = current.split("\n")

    # cur_lines[0] is the oldest line still in the buffer (frozen history).
    # Find where it recurs in baseline and the longest run of shared lines
    # that follows; everything in current after that run is new output.
    best_run = 0
    for d in range(len(base_lines)):
        if base_lines[d] != cur_lines[0]:
            continue
        run = 0
        while d + run < len(base_lines) and run < len(cur_lines) and base_lines[d + run] == cur_lines[run]:
            run += 1
        if run > best_run:
            best_run = run

    if best_run > 0:
        new_lines = _trim_common_tail(cur_lines[best_run:], base_lines)
        result = "\n".join(new_lines).rstrip("\n")
        if result:
            return result

    visible_fallback_lines = 60
    if len(cur_lines) > visible_fallback_lines:
        cur_lines = cur_lines[-visible_fallback_lines:]
    return "\n".join(cur_lines).rstrip("\n")


def _trim_common_tail(new_lines: list[str], base_lines: list[str]) -> list[str]:
    """Drop trailing lines of new_lines that duplicate the tail of base_lines."""

    b = len(base_lines)
    while new_lines and b > 0 and new_lines[-1] == base_lines[b - 1]:
        new_lines = new_lines[:-1]
        b -= 1
    return new_lines


class TmuxSession(AgentSession):
    def __init__(
        self,
        target: str,
        session_id: str | None,
        work_dir: str,
        prompt_pattern: str,
        poll_interval: float,
        strip_input_block: bool,
        strip_patterns: Sequence[str],
    ) -> None:
        self._target = target
        self._session_id = session_id
        self._work_dir = work_dir
        self._prompt_re = re.compile(prompt_pattern) if prompt_pattern else None
        self._poll_interval = poll_interval
        self._strip_input_block = strip_input_block
        self._strip_res = [re.compile(p) for p in strip_patterns]
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._alive = True
        self._poll_task: asyncio.Task[None] | None = None
        self._baseline_capture = ""

    @property
    def current_session_id(self) -> str | None:
        return self._session_id

    def alive(self) -> bool:
        return self._alive

    async def send(
        self,
        prompt: str,
        images: Sequence[ImageAttachment] = (),
        files: Sequence[FileAttachment] = (),
    ) -> None:
        if not self._alive:
            raise RuntimeError("tmux: session closed")

        # Promote images to files so they're saved to disk and referenced by
        # path; the CLI running in the pane can then read them directly.
        all_files = list(files) + [
            FileAttachment(mime_type=img.mime_type, data=img.data, file_name=img.file_name) for img in images
        ]
        if all_files:
            paths = save_files(self._work_dir, all_files)
            if paths:
                prompt = f"{prompt}\n# files: {', '.join(paths)}"

        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()

        # Snapshot the full scrollback before sending so extract_new can diff
        # against exactly what the agent added, regardless of whether the TUI
        # rewrites lines in place or scrolls them.
        self._baseline_capture = await capture_scrollback(self._target)
        visible_baseline = await capture_pane(self._target)

        await send_keys(self._target, prompt)
        self._poll_task = asyncio.create_task(self._poll(visible_baseline))

    async def events(self) -> AsyncIterator[Event]:
        while True:
            event = await self._queue.get()
            yield event
            if event.done:
                return

    async def _poll(self, baseline: str) -> None:
        prev = baseline
        stable = 0
        idle_n = max(10, int(5000 / (self._poll_interval * 1000)))
        try:
            while True:
                await asyncio.sleep(self._poll_interval)
                try:
                    current = await capture_pane(self._target)
                except RuntimeError:
                    continue

                if current == prev:
                    stable += 1
                else:
                    stable = 0
                    prev = current

                # Done: pane stable AND changed from baseline. Fast path:
                # prompt pattern matched. Slow path: idle fallback.
                if stable >= 2 and current != baseline:
                    trimmed = current.rstrip(" \t\n")
                    prompt_ok = self._prompt_re is None or self._prompt_re.search(trimmed) is not None
                    if prompt_ok or stable >= idle_n:
                        response = await self._extract_response()
                        await self._queue.put(Event(type=EventType.RESULT, content=response, done=True))
                        return
        except asyncio.CancelledError:
            pass

    async def _extract_response(self) -> str:
        try:
            current = await capture_scrollback(self._target)
        except RuntimeError:
            current = await capture_pane(self._target)
            return clean_tui_content(current, self._strip_input_block, self._strip_res)

        response = clean_tui_content(
            extract_new(self._baseline_capture, current), self._strip_input_block, self._strip_res
        )
        if response:
            response = f"```\n{response}\n```"
        return response

    async def close(self) -> None:
        self._alive = False
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()


class TmuxAgent(Agent):
    name = "tmux"

    def __init__(
        self,
        session: str,
        pane: str = "0",
        auto_create: bool = True,
        shell: str = "",
        init_command: str = "",
        startup_wait_ms: int = 0,
        prompt_pattern: str = DEFAULT_PROMPT_PATTERN,
        poll_interval_ms: int = 200,
        strip_input_block: bool = True,
        strip_patterns: Sequence[str] | None = None,
    ) -> None:
        if not session:
            raise ValueError("tmux: 'session' option is required (name of the tmux session to attach)")
        if shutil.which("tmux") is None:
            raise ValueError("tmux: 'tmux' not found in PATH")

        self.session_name = session
        self.pane = pane or "0"
        self.auto_create = auto_create
        self.shell = shell
        self.init_command = init_command
        self.startup_wait_ms = startup_wait_ms or (2000 if init_command else 0)
        self.prompt_pattern = prompt_pattern
        self.poll_interval_ms = poll_interval_ms if poll_interval_ms > 0 else 200
        self.strip_input_block = strip_input_block
        self.strip_patterns = list(strip_patterns) if strip_patterns is not None else list(DEFAULT_STRIP_PATTERNS)

    async def start_session(self, session_id: str | None, work_dir: str) -> AgentSession:
        target, window_name = resolve_target(self.session_name, self.pane, work_dir)

        session_exists = await tmux_session_exists(self.session_name)
        window_exists = session_exists and await tmux_window_exists(target)

        if not session_exists:
            if not self.auto_create:
                raise RuntimeError(
                    f"tmux: session {self.session_name!r} does not exist and auto_create is disabled"
                )
            await create_tmux_session(self.session_name, window_name, work_dir, self.shell)
        elif not window_exists and window_name != self.pane:
            await create_tmux_window(self.session_name, window_name, work_dir)

        new_pane = not session_exists or (not window_exists and window_name != self.pane)
        if new_pane:
            # Always cd to the workspace directory so the shell (and any
            # init_command) starts in the right place, regardless of tmux's
            # -c flag behavior.
            if work_dir and work_dir != ".":
                await send_keys(target, f"cd {_shell_quote(work_dir)}")
            if self.init_command:
                await send_keys(target, self.init_command)
                if self.startup_wait_ms > 0:
                    await asyncio.sleep(self.startup_wait_ms / 1000)

        return TmuxSession(
            target=target,
            session_id=session_id,
            work_dir=work_dir,
            prompt_pattern=self.prompt_pattern,
            poll_interval=self.poll_interval_ms / 1000,
            strip_input_block=self.strip_input_block,
            strip_patterns=self.strip_patterns,
        )
