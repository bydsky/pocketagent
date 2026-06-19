"""OpenAI Codex CLI agent backend.

Unlike Claude Code's persistent stdin/stdout stream-json process, `codex exec`
is one-shot: each invocation runs a single turn to completion and exits, so
this backend spawns a fresh subprocess per `send()` rather than holding one
open for the life of the session:

    codex exec --json --skip-git-repo-check [--sandbox <mode>] \
               [--ask-for-approval <mode>] [--model <m>] \
               [-c developer_instructions="<text>"] [--image <p1,p2,...>] \
               [resume <thread_id>] "<prompt>"

--json turns stdout into a JSON-Lines event stream (one object per line);
human-readable progress goes to stderr, which is drained and discarded so it
can't fill its pipe and stall the process. Key event shapes:

    {"type": "thread.started", "thread_id": "..."}
    {"type": "item.completed", "item": {"type": "agent_message", "text": "..."}}
    {"type": "turn.completed", "usage": {"input_tokens": .., "output_tokens": ..}}
    {"type": "turn.failed", "error": {"message": "..."}}
    {"type": "error", "message": "..."}

thread_id (carried only on thread.started) is this backend's session id, fed
back via `resume <thread_id>` on the next turn. There is no
--append-system-prompt equivalent; `-c developer_instructions=<toml-string>`
(a conversation message appended to the default system prompt) is the
closest analog and is used the same way claude_code.py uses
--append-system-prompt, combining agent_system_prompt and
platform_system_prompt.

ask_for_approval defaults to "never": codex exec has no stdio permission
-prompt-tool protocol to answer approval requests the way Claude Code does,
so non-interactive use must avoid ever pausing for one (see Codex's own
non-interactive-mode guidance).

v1 limitation: only agent_message/reasoning item types are translated into
TEXT/THINKING events with confirmed field shapes; other item types
(command_execution, file_change, mcp_tool_call, web_search) are surfaced as
generic TOOL_USE events carrying the whole item as JSON, since their exact
field-by-field schema isn't part of Codex's documented --json contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Sequence

from ..core.agent import Agent, AgentSession
from ..core.attachments import save_files
from ..core.types import Event, EventType, FileAttachment, ImageAttachment

logger = logging.getLogger(__name__)

_KNOWN_ITEM_TYPES = {"command_execution", "file_change", "mcp_tool_call", "web_search"}


def translate_message(msg: dict[str, Any]) -> list[Event]:
    """Pure translation of one parsed `codex exec --json` line into zero or more Events.

    Kept separate from the subprocess/I-O plumbing so it can be unit tested
    without spawning a real `codex` process.
    """

    msg_type = msg.get("type")

    if msg_type == "thread.started":
        return [
            Event(
                type=EventType.THINKING,
                content="session started",
                session_id=msg.get("thread_id"),
            )
        ]

    if msg_type == "item.completed":
        item = msg.get("item") or {}
        item_type = item.get("type")
        if item_type == "agent_message" and item.get("text"):
            return [Event(type=EventType.TEXT, content=item["text"])]
        if item_type == "reasoning" and (item.get("text") or item.get("summary")):
            return [Event(type=EventType.THINKING, content=item.get("text") or item.get("summary", ""))]
        if item_type in _KNOWN_ITEM_TYPES:
            return [
                Event(type=EventType.TOOL_USE, tool_name=item_type, tool_input=json.dumps(item))
            ]
        return []

    if msg_type == "turn.completed":
        usage = msg.get("usage") or {}
        return [
            Event(
                type=EventType.RESULT,
                done=True,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            )
        ]

    if msg_type == "turn.failed":
        error = msg.get("error")
        message = error.get("message") if isinstance(error, dict) else str(error or "turn failed")
        return [Event(type=EventType.ERROR, error=message, done=True)]

    if msg_type == "error":
        return [Event(type=EventType.ERROR, error=msg.get("message", "codex error"), done=True)]

    return []


def _toml_quote(s: str) -> str:
    """Render s as a single-line TOML basic string for use in `-c key=<value>`."""

    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def _combine_system_prompts(agent_system_prompt: str, platform_system_prompt: str) -> str:
    parts = [p for p in (agent_system_prompt, platform_system_prompt) if p]
    return "\n\n".join(parts)


def build_exec_args(
    *,
    session_id: str | None,
    sandbox: str,
    ask_for_approval: str,
    model: str,
    system_prompt: str,
    image_paths: Sequence[str],
    extra_args: Sequence[str],
    skip_git_repo_check: bool,
    prompt: str,
) -> list[str]:
    args = ["exec", "--json"]
    if skip_git_repo_check:
        args.append("--skip-git-repo-check")
    if sandbox:
        args += ["--sandbox", sandbox]
    if ask_for_approval:
        args += ["--ask-for-approval", ask_for_approval]
    if model:
        args += ["--model", model]
    if system_prompt:
        args += ["-c", f"developer_instructions={_toml_quote(system_prompt)}"]
    if image_paths:
        args += ["--image", ",".join(image_paths)]
    args += list(extra_args)
    if session_id:
        args += ["resume", session_id]
    args.append(prompt)
    return args


def _build_user_text(prompt: str, file_paths: Sequence[str]) -> str:
    if not file_paths:
        return prompt
    refs = ", ".join(file_paths)
    if prompt:
        return f"{prompt}\n\n(Files saved locally, please read them: {refs})"
    return f"Please analyze the attached file(s): {refs}"


class CodexSession(AgentSession):
    def __init__(
        self,
        command: str,
        work_dir: str,
        session_id: str | None,
        sandbox: str,
        ask_for_approval: str,
        model: str,
        system_prompt: str,
        extra_args: Sequence[str],
        skip_git_repo_check: bool,
    ) -> None:
        self._command = command
        self._work_dir = work_dir
        self._session_id = session_id
        self._sandbox = sandbox
        self._ask_for_approval = ask_for_approval
        self._model = model
        self._system_prompt = system_prompt
        self._extra_args = extra_args
        self._skip_git_repo_check = skip_git_repo_check
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._alive = True
        self._process: asyncio.subprocess.Process | None = None
        self._turn_task: asyncio.Task[None] | None = None

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
            raise RuntimeError("codex: session closed")

        file_paths = save_files(self._work_dir, files)
        image_paths = save_files(
            self._work_dir,
            [
                FileAttachment(mime_type=img.mime_type, data=img.data, file_name=img.file_name or f"image_{i}.png")
                for i, img in enumerate(images)
            ],
        )
        text = _build_user_text(prompt, file_paths)

        args = build_exec_args(
            session_id=self._session_id,
            sandbox=self._sandbox,
            ask_for_approval=self._ask_for_approval,
            model=self._model,
            system_prompt=self._system_prompt,
            image_paths=image_paths,
            extra_args=self._extra_args,
            skip_git_repo_check=self._skip_git_repo_check,
            prompt=text,
        )

        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()

        self._process = await asyncio.create_subprocess_exec(
            self._command,
            *args,
            cwd=self._work_dir,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._turn_task = asyncio.create_task(self._run_turn(self._process))

    async def events(self) -> AsyncIterator[Event]:
        while True:
            event = await self._queue.get()
            yield event
            if event.done:
                return

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        try:
            while True:
                raw = await process.stderr.readline()
                if not raw:
                    break
                logger.debug("codex: %s", raw.decode("utf-8", errors="replace").rstrip())
        except asyncio.CancelledError:
            pass

    async def _run_turn(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        stderr_task = asyncio.create_task(self._drain_stderr(process))
        saw_done = False
        try:
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("thread_id"):
                    self._session_id = msg["thread_id"]
                for event in translate_message(msg):
                    if event.done:
                        saw_done = True
                    await self._queue.put(event)
            await process.wait()
            if not saw_done:
                await self._queue.put(
                    Event(
                        type=EventType.ERROR,
                        error=f"codex exited unexpectedly (code {process.returncode})",
                        done=True,
                    )
                )
        except asyncio.CancelledError:
            pass
        finally:
            stderr_task.cancel()

    async def close(self) -> None:
        self._alive = False
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()


class CodexAgent(Agent):
    name = "codex"

    def __init__(
        self,
        command: str = "codex",
        model: str = "",
        sandbox: str = "workspace-write",
        ask_for_approval: str = "never",
        extra_args: Sequence[str] = (),
        agent_system_prompt: str = "",
        skip_git_repo_check: bool = True,
    ) -> None:
        self.command = command
        self.model = model
        self.sandbox = sandbox
        self.ask_for_approval = ask_for_approval
        self.extra_args = list(extra_args)
        self.agent_system_prompt = agent_system_prompt
        self.skip_git_repo_check = skip_git_repo_check

    async def start_session(
        self, session_id: str | None, work_dir: str, platform_system_prompt: str = ""
    ) -> AgentSession:
        system_prompt = _combine_system_prompts(self.agent_system_prompt, platform_system_prompt)
        return CodexSession(
            command=self.command,
            work_dir=work_dir,
            session_id=session_id,
            sandbox=self.sandbox,
            ask_for_approval=self.ask_for_approval,
            model=self.model,
            system_prompt=system_prompt,
            extra_args=self.extra_args,
            skip_git_repo_check=self.skip_git_repo_check,
        )
