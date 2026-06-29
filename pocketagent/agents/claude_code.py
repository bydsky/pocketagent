"""Claude Code CLI agent backend.

Drives `claude` as a persistent subprocess using its bidirectional
stream-json protocol:

    claude --print --verbose --input-format stream-json --output-format stream-json \
           --permission-prompt-tool stdio [--resume <id>] [--model <m>] \
           [--permission-mode <mode>] [--append-system-prompt <text>]

--print is required for non-interactive/stream-json mode at all (without
it, claude starts its interactive TUI, which immediately exits with no
TTY attached); --verbose is required by --output-format=stream-json when
combined with --print. --append-system-prompt, if present, carries this
agent's own agent_system_prompt (a config option) combined with the calling
platform's platform_system_prompt, joined by a blank line.

Each user turn is written to stdin as one JSON line:
    {"type": "user", "message": {"role": "user", "content": <str-or-blocks>}}

stdout emits one JSON object per line:
    {"type": "system", "subtype": "init", "session_id": "...", ...}
    {"type": "assistant", "message": {"content": [...]}, "session_id": "..."}
    {"type": "control_request", "request_id": "...", "request": {...}}
    {"type": "result", "result": "...", "session_id": "...", "usage": {...}}

v1 limitation: permission control_requests are auto-approved (no interactive
allow/deny UI wired into chat yet); a PERMISSION_REQUEST event is still
emitted so callers can log/observe it.

The resolved model name (e.g. "claude-sonnet-4-6") only appears on
`assistant` messages, not on `result`; ClaudeCodeSession tracks the latest
one seen and stamps a display-formatted version (_format_model_name, e.g.
"Sonnet 4.6") onto the RESULT event so the engine can show it in a reply
footer without needing any claude-specific formatting logic itself. `result`
does carry `total_cost_usd` directly, so that one maps straight through in
translate_message.

The CLI's own `--effort <level>` flag (low/medium/high/xhigh/max) is never
echoed back anywhere in the stream-json protocol (confirmed: absent from
`system`/`init`, `assistant`, and `result` messages alike), so unlike model
there's nothing to read off the stream -- ClaudeCodeSession just stamps back
the same effort string it was constructed with.

The interactive CLI's own status line shows a context_window.used_percentage
that has no equivalent field on `result` -- confirmed by configuring a
statusLine command and running claude in this exact --print/stream-json mode:
the hook is never invoked, since it only fires from the interactive render
loop. _compute_context_used_pct replicates the CLI's own formula instead
(input + cache_creation_input + cache_read_input tokens, over
modelUsage[model].contextWindow), verified against a real statusLine payload.

Rate-limit percentages (5h/7d usage) are likewise statusLine-only data with no
JSON field anywhere in this protocol -- but the CLI's own `/usage` slash
command reports them as plain text and is handled client-side (no API call:
total_cost_usd 0, model "<synthetic>"). When show_footer is set (the channel
is configured to display the reply footer -- see core/router.py), ClaudeCodeSession
fetches `/usage` from a brand-new, un-resumed claude process after every real
turn and parses its reply (_parse_usage_text) -- NOT by sending it on the live
session's own stdin: an earlier version did that, which injects a "/usage"
exchange into this session's --resume transcript, and the model would then
sometimes treat a later, unrelated user message as commentary on that earlier
exchange instead of answering it. A throwaway process avoids polluting the
transcript at the cost of spawning one extra subprocess per reply. Channels
with the footer off skip this fetch entirely since nothing would display the
numbers anyway.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.agent import Agent, AgentSession
from ..core.attachments import save_files
from ..core.utils import format_duration as _format_duration
from ..core.scheduler import next_occurrence, resolve_timezone
from ..core.types import Event, EventType, FileAttachment, ImageAttachment

logger = logging.getLogger(__name__)

# asyncio.create_subprocess_exec defaults stdout's line-reader limit to 64 KiB;
# a single stream-json line (e.g. an assistant message carrying a large tool
# result) can exceed that, raising LimitOverrunError out of readline(). Without
# a larger limit that exception used to go uncaught and silently kill the
# reader task while the claude subprocess itself stayed alive -- see
# ClaudeCodeSession._read_loop.
_STDOUT_LIMIT = 16 * 1024 * 1024


def translate_message(msg: dict[str, Any]) -> list[Event]:
    """Pure translation of one parsed stream-json line into zero or more Events.

    Kept separate from the subprocess/I-O plumbing so it can be unit tested
    without spawning a real `claude` process.
    """

    msg_type = msg.get("type")
    session_id = msg.get("session_id")

    if msg_type == "system":
        return [Event(type=EventType.THINKING, content="session started", session_id=session_id)]

    if msg_type == "assistant":
        events: list[Event] = []
        content = (msg.get("message") or {}).get("content") or []
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content:
            block_type = block.get("type")
            if block_type == "text" and block.get("text"):
                events.append(
                    Event(type=EventType.TEXT, content=block["text"], session_id=session_id)
                )
            elif block_type == "thinking" and block.get("thinking"):
                events.append(
                    Event(
                        type=EventType.THINKING,
                        content=block["thinking"],
                        session_id=session_id,
                    )
                )
            elif block_type == "tool_use":
                events.append(
                    Event(
                        type=EventType.TOOL_USE,
                        tool_name=block.get("name", ""),
                        tool_input=json.dumps(block.get("input", {})),
                        session_id=session_id,
                    )
                )
        return events

    if msg_type == "control_request":
        request = msg.get("request") or {}
        return [
            Event(
                type=EventType.PERMISSION_REQUEST,
                tool_name=request.get("tool_name", ""),
                tool_input=json.dumps(request.get("input", {})),
                request_id=msg.get("request_id"),
                session_id=session_id,
            )
        ]

    if msg_type == "result":
        usage = msg.get("usage") or {}
        is_error = bool(msg.get("is_error"))
        # A failed --resume (e.g. "No conversation found with session ID: ...")
        # has no "result" text -- the reason lives in "errors" instead -- and
        # echoes back the same (invalid) session_id we asked it to resume. Not
        # persisting session_id on error results stops that bad id from being
        # written back to sessions.json and permanently wedging the channel on
        # every future message.
        error_text = msg.get("result") or "; ".join(msg.get("errors") or []) or None
        return [
            Event(
                type=EventType.ERROR if is_error else EventType.RESULT,
                content=msg.get("result", ""),
                session_id=None if is_error else session_id,
                done=True,
                error=error_text if is_error else None,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cost_usd=msg.get("total_cost_usd"),
            )
        ]

    return []


def _compute_context_used_pct(result_msg: dict[str, Any], model: str) -> int | None:
    """Replicate the interactive CLI's own context_window.used_percentage formula.

    That field is only computed by the interactive statusLine hook (confirmed by
    spawning claude with --print and a statusLine command configured: the hook
    never fires in headless/stream-json mode), so it's not on the result message
    directly. But its inputs are: result_msg.usage.{input,cache_creation_input,
    cache_read_input}_tokens summed, divided by modelUsage[model].contextWindow --
    verified against a real statusLine payload where 3+71048+12454=83505 input
    tokens over a 600000 context_window_size rounds to the reported 14%.
    """

    context_window = ((result_msg.get("modelUsage") or {}).get(model) or {}).get("contextWindow")
    if not context_window:
        return None
    usage = result_msg.get("usage") or {}
    used_tokens = (
        usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
    )
    return round(used_tokens / context_window * 100)


_MODEL_ID_RE = re.compile(r"^claude-([a-z]+)-(\d+)-(\d+)(?:-\d+)?$")


def _format_model_name(model: str) -> str:
    """Turn a raw model id like "claude-sonnet-4-6" into "Sonnet 4.6" for display.

    Falls back to the raw id unchanged for anything that doesn't match this
    shape (future claude id formats). Only applied at the point a model id is
    stamped onto an Event for display -- _compute_context_used_pct still needs
    the raw id to look it up in modelUsage, so self._model itself stays raw.
    """

    match = _MODEL_ID_RE.match(model)
    if not match:
        return model
    family, major, minor = match.groups()
    return f"{family.capitalize()} {major}.{minor}"


_RESET_CLAUSE = r"(?:\s*·\s*resets ([A-Za-z]{3} \d{1,2}, \d{1,2}(?::\d{2})?(?:am|pm)) \(([^)]+)\))?"
_USAGE_SESSION_RE = re.compile(rf"Current session:\s*(\d+)%\s*used{_RESET_CLAUSE}")
_USAGE_WEEK_RE = re.compile(rf"Current week \(all models\):\s*(\d+)%\s*used{_RESET_CLAUSE}")


def _parse_reset_in(date_str: str | None, tz_name: str | None, now: datetime) -> str | None:
    """Turn a "Jun 19, 2:29pm" + IANA tz name (as the CLI's `/usage` reply
    reports resets) into a compact countdown relative to now, a tz-aware
    reference instant. now is a parameter (rather than datetime.now() inline)
    so tests can pin "the present" without mocking the clock.

    The CLI drops the ":00" when a reset lands exactly on the hour (e.g.
    "Jun 23, 8pm" rather than "Jun 23, 8:00pm") -- the 7-day reset in
    particular tends to land on a fixed clock time and hits this far more
    often than the 5-hour one, so without normalizing it here the 7d
    countdown went missing far more than the 5h one did.
    """

    if not date_str or not tz_name:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    if ":" not in date_str:
        date_str = re.sub(r"(\d+)(am|pm)$", r"\1:00\2", date_str)
    naive = datetime.strptime(f"{date_str} 2000", "%b %d, %I:%M%p %Y")
    now_local = now.astimezone(tz)
    target = naive.replace(year=now_local.year, tzinfo=tz)
    if target < now_local:
        target = target.replace(year=now_local.year + 1)
    return _format_duration(target - now_local)


def _parse_usage_text(
    text: str, now: datetime | None = None
) -> tuple[int | None, str | None, int | None, str | None]:
    """Parse the plain-text reply from the CLI's own `/usage` slash command.

    `/usage` is handled entirely client-side (no model call: total_cost_usd is
    0 and the reported model is "<synthetic>") and is the only place rate-limit
    percentages (and their reset times) are obtainable in --print/stream-json
    mode -- they're not on any JSON message field, and the interactive
    statusLine hook that normally reports them never fires here (confirmed by
    configuring one and observing it's never invoked under --print). Returns
    (five_hour_pct, five_hour_reset_in, seven_day_pct, seven_day_reset_in).
    """

    now = now or datetime.now(timezone.utc)
    five_hour = _USAGE_SESSION_RE.search(text)
    seven_day = _USAGE_WEEK_RE.search(text)
    return (
        int(five_hour.group(1)) if five_hour else None,
        _parse_reset_in(five_hour.group(2), five_hour.group(3), now) if five_hour else None,
        int(seven_day.group(1)) if seven_day else None,
        _parse_reset_in(seven_day.group(2), seven_day.group(3), now) if seven_day else None,
    )


_LIMIT_DENIED_RE = re.compile(
    r"hit your \w+ limit\D*?resets\s+(\d{1,2}:\d{2}\s*[ap]m)\s*\(([^)]+)\)",
    re.IGNORECASE,
)


def _parse_limit_denied(error_text: str, now: datetime | None = None) -> datetime | None:
    """Parse claude_code's own usage-limit-denial error text -- e.g. "You've hit
    your session limit · resets 2:50pm (Australia/Sydney)" -- into the absolute
    instant it next resets (today or tomorrow, in the denial's own timezone), or
    None if error_text isn't this kind of denial.

    This wording is specific to claude_code's CLI, so the parsing lives here
    rather than in core/utils.py -- core/engine.py only ever reads the
    already-parsed Event.rate_limit_retry_at field this stamps, never this
    function or its regex, so adding another agent backend with differently
    worded denials needs no engine.py changes.

    Falls back to UTC if the timezone name is missing/unrecognized, so the
    result is always tz-aware (never the "naive local time" fallback that
    resolve_timezone/next_occurrence use elsewhere for daily_reset, which
    would make it unsafe to compare against the proactive 100%-usage signal's
    UTC instant in Engine).
    """

    match = _LIMIT_DENIED_RE.search(error_text)
    if not match:
        return None
    time_str, tz_name = match.group(1), match.group(2)
    tz = resolve_timezone(tz_name) or timezone.utc
    try:
        target = datetime.strptime(time_str.replace(" ", "").upper(), "%I:%M%p").time()
    except ValueError:
        return None
    # now, if given, may be in any tz (e.g. UTC) -- convert into tz first so
    # replacing hour/minute lands on the right wall-clock instant.
    now_in_tz = now.astimezone(tz) if now is not None else None
    return next_occurrence(target, tz, now_in_tz)


def _build_user_message(
    prompt: str, images: Sequence[ImageAttachment], file_paths: Sequence[str]
) -> dict[str, Any]:
    text = prompt
    if file_paths:
        refs = ", ".join(file_paths)
        text = f"{text}\n\n(Files saved locally, please read them: {refs})" if text else (
            f"Please analyze the attached file(s): {refs}"
        )

    if not images:
        return {"type": "user", "message": {"role": "user", "content": text}}

    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    for img in images:
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.mime_type or "image/png",
                    "data": base64.b64encode(img.data).decode("ascii"),
                },
            }
        )
    return {"type": "user", "message": {"role": "user", "content": blocks}}


def _save_files(work_dir: str, files: Sequence[FileAttachment]) -> list[str]:
    return save_files(work_dir, files)


class ClaudeCodeSession(AgentSession):
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        work_dir: str,
        show_footer: bool = False,
        command: str = "claude",
        effort: str = "",
    ) -> None:
        self._process = process
        self._work_dir = work_dir
        self._show_footer = show_footer
        self._command = command
        self._effort = effort
        self._session_id: str | None = None
        self._model: str = ""
        self._dead = False
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._reader_task = asyncio.create_task(self._read_loop())

    @property
    def current_session_id(self) -> str | None:
        return self._session_id

    def alive(self) -> bool:
        return self._process.returncode is None and not self._dead

    async def send(
        self,
        prompt: str,
        images: Sequence[ImageAttachment] = (),
        files: Sequence[FileAttachment] = (),
    ) -> None:
        file_paths = _save_files(self._work_dir, files)
        message = _build_user_message(prompt, images, file_paths)
        line = json.dumps(message) + "\n"
        assert self._process.stdin is not None
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()

    async def events(self) -> AsyncIterator[Event]:
        while True:
            event = await self._queue.get()
            yield event
            if event.done:
                return

    async def _read_loop(self) -> None:
        assert self._process.stdout is not None
        try:
            while True:
                raw = await self._process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("session_id"):
                    self._session_id = msg["session_id"]
                if msg.get("type") == "assistant":
                    model = (msg.get("message") or {}).get("model")
                    if model:
                        self._model = model
                for event in translate_message(msg):
                    if event.type == EventType.PERMISSION_REQUEST:
                        await self._auto_approve(event.request_id)
                    elif event.type == EventType.ERROR and event.error:
                        event.rate_limit_retry_at = _parse_limit_denied(event.error)
                    elif event.type == EventType.RESULT:
                        event.model = _format_model_name(self._model)
                        event.effort = self._effort
                        event.context_used_pct = _compute_context_used_pct(msg, self._model)
                        if self._show_footer:
                            (
                                event.rate_limit_5h_pct,
                                event.rate_limit_5h_reset_in,
                                event.rate_limit_7d_pct,
                                event.rate_limit_7d_reset_in,
                            ) = await self._fetch_rate_limits()
                    await self._queue.put(event)
        except asyncio.CancelledError:
            return
        except Exception:
            # A line over _STDOUT_LIMIT (LimitOverrunError) or any other read
            # failure used to leave the claude subprocess running but with no
            # task left consuming its stdout -- alive() still said yes, so
            # SessionStore kept reusing this session forever and every future
            # events() call hung on an empty queue. Marking _dead and killing
            # the subprocess here ensures the next message spawns a fresh one.
            logger.exception("claude_code: reader loop crashed, resetting session")
            self._dead = True
            if self._process.returncode is None:
                self._process.terminate()
            await self._queue.put(
                Event(
                    type=EventType.ERROR,
                    error="agent session hit an internal error and was reset -- please retry",
                    done=True,
                )
            )
            return
        if self._process.returncode is None:
            await self._queue.put(
                Event(type=EventType.ERROR, error="agent process ended unexpectedly", done=True)
            )

    async def _fetch_rate_limits(self) -> tuple[int | None, str | None, int | None, str | None]:
        """Send `/usage` to a brand-new, un-resumed claude process and parse its
        plain-text reply.

        This used to send `/usage` as an extra turn on the live session's own
        stdin -- but that injects a "/usage" exchange into this session's
        --resume transcript, which the model then sees on every later turn.
        In practice that confused the model into treating a subsequent,
        unrelated user message as commentary on the earlier /usage exchange
        (replying with something like "no response needed, that's just a
        local /usage command output" instead of answering the actual
        question). Since /usage is account-level and client-side (no model
        call: total_cost_usd 0, model "<synthetic>"), a throwaway process
        with no --resume reports the same numbers without that side effect.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                self._command,
                "--print",
                "--verbose",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--permission-prompt-tool", "stdio",
                cwd=self._work_dir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=_STDOUT_LIMIT,
            )
        except OSError:
            return None, None, None, None
        try:
            assert proc.stdin is not None
            line = json.dumps({"type": "user", "message": {"role": "user", "content": "/usage"}}) + "\n"
            proc.stdin.write(line.encode("utf-8"))
            await proc.stdin.drain()
            return await asyncio.wait_for(self._read_usage_result(proc), timeout=15)
        except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError):
            return None, None, None, None
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()

    async def _read_usage_result(
        self, proc: asyncio.subprocess.Process
    ) -> tuple[int | None, str | None, int | None, str | None]:
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                return None, None, None, None
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "result":
                return _parse_usage_text(msg.get("result", ""))

    async def _auto_approve(self, request_id: str | None) -> None:
        if not request_id or self._process.stdin is None:
            return
        response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": {"behavior": "allow"},
            },
        }
        self._process.stdin.write((json.dumps(response) + "\n").encode("utf-8"))
        await self._process.stdin.drain()

    async def close(self) -> None:
        self._reader_task.cancel()
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()


def _combine_system_prompts(agent_system_prompt: str, platform_system_prompt: str) -> str:
    parts = [p for p in (agent_system_prompt, platform_system_prompt) if p]
    return "\n\n".join(parts)


class ClaudeCodeAgent(Agent):
    name = "claude_code"

    def __init__(
        self,
        command: str = "claude",
        model: str = "",
        permission_mode: str = "default",
        effort: str = "",
        extra_args: Sequence[str] = (),
        agent_system_prompt: str = "",
    ) -> None:
        self.command = command
        self.model = model
        self.permission_mode = permission_mode
        self.effort = effort
        self.extra_args = list(extra_args)
        self.agent_system_prompt = agent_system_prompt

    async def start_session(
        self,
        session_id: str | None,
        work_dir: str,
        platform_system_prompt: str = "",
        show_footer: bool = False,
    ) -> AgentSession:
        args = [
            "--print",
            "--verbose",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--permission-prompt-tool", "stdio",
        ]
        if self.permission_mode:
            args += ["--permission-mode", self.permission_mode]
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]
        if session_id:
            args += ["--resume", session_id]
        system_prompt = _combine_system_prompts(self.agent_system_prompt, platform_system_prompt)
        if system_prompt:
            args += ["--append-system-prompt", system_prompt]
        args += self.extra_args

        process = await asyncio.create_subprocess_exec(
            self.command,
            *args,
            cwd=work_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STDOUT_LIMIT,
        )
        return ClaudeCodeSession(process, work_dir, show_footer, self.command, self.effort)
