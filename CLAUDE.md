# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

pocketagent bridges chat platforms (Discord, with Slack/Telegram planned) to AI coding
agent CLIs (Claude Code, with Gemini CLI/tmux planned), so a coding agent can be driven
from a chat app. Each platform has a default agent plus optional per-channel-id overrides
(different agent and/or workspace per channel). Every channel gets its own workspace
folder; folder bindings persist in `.pocketagent-bindings.json` so renaming a channel
doesn't orphan its history. Conversation continuity survives restarts: the agent's own
session id is persisted per channel/user and passed back via `--resume`.

Custom commands (config-defined prompt templates or shell commands) let `/deploy api prod`
expand a template like `"Deploy {{1}} to the {{2:staging}} environment..."` into a normal
prompt, or run a literal shell command in the channel's workspace dir. See
`config.example.toml` for the full config shape.

## Commands

```bash
pip install -e ".[dev]"          # install package + pytest/pytest-asyncio
pytest                            # run the full test suite
pytest tests/test_router.py       # run one test file
pytest tests/test_router.py::test_name -v   # run a single test

cp config.example.toml pocketagent.toml   # then edit: Discord token, base_dir, etc.
pocketagent run -c pocketagent.toml       # run the bridge (requires `claude` CLI on PATH)
pocketagent run -c pocketagent.toml -v    # with debug logging
```

`requires-python = ">=3.11"`. No lint/format tooling is configured.

## Architecture

Message flow: `Platform.start(handler)` receives a chat message -> wraps it in a
platform-agnostic `Message` -> `Engine.on_message` -> `CommandRegistry.expand` (custom
`/command` rewrite, if matched) -> `Router.resolve` (which agent + workspace dir for this
channel) -> `SessionStore.get_or_create` (reuse a live `AgentSession` or resume one from a
persisted session id) -> `AgentSession.send` + `AgentSession.events()` streamed back ->
`Platform.reply`.

Everything platform/agent-specific is hidden behind two ABCs in `pocketagent/core/`:

- **`Platform`** (`core/platform.py`): `start(handler)`, `reply(reply_ctx, content)`,
  `send(reply_ctx, content)`, `stop()`. One implementation per chat platform lives in
  `pocketagent/platforms/` (currently `discord_platform.py`).
- **`Agent`** / **`AgentSession`** (`core/agent.py`): `Agent.start_session(session_id,
  work_dir)` returns an `AgentSession` with `send(prompt, images, files)`,
  `events()` (async iterator of `Event`), `alive()`, `close()`. One implementation per
  agent backend lives in `pocketagent/agents/` (`claude_code.py`, `tmux.py`).

Adding a new platform or agent means adding one module implementing one of these
interfaces — `core/engine.py` does not need to change.

Supporting pieces in `core/`:

- **`router.py`**: resolves `(agent_name, work_dir)` for a channel. A platform has one
  `default_agent`; per-channel-id `ChannelOverride`s can pin a different agent and/or a
  fixed workspace folder name.
- **`workspace.py`** (`WorkspaceManager`): maps a channel to a sanitized, collision-free
  directory name under the platform's `base_dir`, persisting the mapping in
  `.pocketagent-bindings.json` so a later channel rename doesn't orphan the workspace.
- **`session_store.py`**: caches live `AgentSession`s by `session_key` and persists each
  session's agent-reported `session_id` to disk (`state_dir/sessions.json`) so a restart
  can `--resume` instead of starting fresh.
- **`commands.py`**: parses `/name arg1 arg2`, expands `{{1}}`, `{{N:default}}`, `{{N*}}`,
  `{{args}}` placeholders against a configured prompt template (or appends args to an
  `exec` shell command — exec args are not template-expanded, just appended).
- **`textsplit.py`**: splits long agent replies into platform-size chunks without breaking
  a message across an open fenced code block.
- **`types.py`**: the shared `Message`, `Event`/`EventType`, `ImageAttachment`,
  `FileAttachment` dataclasses passed between platforms, the engine, and agents.
- **`attachments.py`**: `save_files(work_dir, files)` writes attachments into
  `work_dir/.pocketagent/attachments`, used by both agent backends.

`config.py` loads `pocketagent.toml` (via `tomllib`) into `AppConfig`/`PlatformConfig` and
wires up the concrete `Agent`/`Platform` instances and `Engine` (`build_app`). New agent or
platform types register themselves in `AGENT_FACTORIES` / `PLATFORM_FACTORIES` there.

### Claude Code agent backend (`agents/claude_code.py`)

Drives `claude` as a persistent subprocess speaking the bidirectional stream-json protocol
(`--input-format stream-json --output-format stream-json --permission-prompt-tool stdio`,
plus `--resume <id>` / `--model` / `--permission-mode` as configured). Each user turn is one
JSON line on stdin; stdout emits one JSON object per line (`system`/`init`, `assistant`,
`control_request`, `result`). `translate_message()` is a pure function from one parsed
stream-json line to zero or more `Event`s, kept separate from the subprocess plumbing so
it's unit-testable without spawning a real `claude` process (see
`tests/test_claude_code_protocol.py`). Permission `control_request`s are currently
auto-approved (no interactive allow/deny UI wired into chat yet); a `PERMISSION_REQUEST`
event is still emitted so callers can observe it.

### tmux agent backend (`agents/tmux.py`)

Drives a persistent tmux pane as an interactive shell agent for CLIs that only offer a
TUI (e.g. `claude` run as `init_command` rather than via stream-json). A user message is
sent as literal keystrokes (`tmux send-keys -l ... Enter`); the reply is captured by
polling `capture-pane` until the pane is stable — and, if it matches `prompt_pattern`,
idle on a prompt — then diffing the capture against a baseline scrollback snapshot taken
right before the keystrokes were sent (`extract_new` handles both linear shell output and
TUI redraw/scroll cases). Each distinct `work_dir` gets its own tmux window inside the
configured `session`, named after the directory and hash-suffixed (`unique_window_name`)
so two dirs sharing a basename never collide.

### Discord platform (`platforms/discord_platform.py`)

DMs are always dispatched; guild channel messages only dispatch when the bot is
`@`-mentioned (the mention is stripped before handing the message to the engine). No
threads/buttons/slash-commands yet — plain text messaging only.
