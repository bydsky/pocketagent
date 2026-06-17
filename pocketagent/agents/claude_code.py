"""Claude Code CLI agent backend.

Drives `claude` as a persistent subprocess using its bidirectional
stream-json protocol:

    claude --input-format stream-json --output-format stream-json \
           --permission-prompt-tool stdio [--resume <id>] [--model <m>] \
           [--permission-mode <mode>]

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
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

from ..core.agent import Agent, AgentSession
from ..core.types import Event, EventType, FileAttachment, ImageAttachment

ATTACHMENTS_DIRNAME = ".pocketagent/attachments"


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
        return [
            Event(
                type=EventType.ERROR if is_error else EventType.RESULT,
                content=msg.get("result", ""),
                session_id=session_id,
                done=True,
                error=msg.get("result") if is_error else None,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            )
        ]

    return []


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
    if not files:
        return []
    attach_dir = Path(work_dir) / ATTACHMENTS_DIRNAME
    attach_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, f in enumerate(files):
        name = Path(f.file_name or f"file_{i}").name or f"file_{i}"
        path = attach_dir / name
        path.write_bytes(f.data)
        paths.append(str(path))
    return paths


class ClaudeCodeSession(AgentSession):
    def __init__(self, process: asyncio.subprocess.Process, work_dir: str) -> None:
        self._process = process
        self._work_dir = work_dir
        self._session_id: str | None = None
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._reader_task = asyncio.create_task(self._read_loop())

    @property
    def current_session_id(self) -> str | None:
        return self._session_id

    def alive(self) -> bool:
        return self._process.returncode is None

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
                for event in translate_message(msg):
                    if event.type == EventType.PERMISSION_REQUEST:
                        await self._auto_approve(event.request_id)
                    await self._queue.put(event)
        except asyncio.CancelledError:
            pass
        finally:
            if self._process.returncode is None:
                await self._queue.put(
                    Event(type=EventType.ERROR, error="agent process ended unexpectedly", done=True)
                )

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


class ClaudeCodeAgent(Agent):
    name = "claude_code"

    def __init__(
        self,
        command: str = "claude",
        model: str = "",
        permission_mode: str = "default",
        extra_args: Sequence[str] = (),
    ) -> None:
        self.command = command
        self.model = model
        self.permission_mode = permission_mode
        self.extra_args = list(extra_args)

    async def start_session(self, session_id: str | None, work_dir: str) -> AgentSession:
        args = [
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--permission-prompt-tool", "stdio",
        ]
        if self.permission_mode:
            args += ["--permission-mode", self.permission_mode]
        if self.model:
            args += ["--model", self.model]
        if session_id:
            args += ["--resume", session_id]
        args += self.extra_args

        process = await asyncio.create_subprocess_exec(
            self.command,
            *args,
            cwd=work_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return ClaudeCodeSession(process, work_dir)
