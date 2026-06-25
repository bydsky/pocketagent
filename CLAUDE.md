# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

pocketagent bridges chat platforms (Discord, Telegram, Slack) to AI coding
agent CLIs (Claude Code, Codex, tmux, with Gemini CLI planned), so a coding agent can be
driven from a chat app. Each platform has a default agent plus optional per-channel-id overrides
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
`Platform.reply`. The agent-waiting part (and the `exec`-command branch) runs inside
`async with platform.typing(msg.reply_ctx):` so platforms with a native "working"
indicator (Discord's typing dots) can show one for the duration.

Everything platform/agent-specific is hidden behind two ABCs in `pocketagent/core/`:

- **`Platform`** (`core/platform.py`): `start(handler)`, `reply(reply_ctx, content)`,
  `send(reply_ctx, content)`, `stop()`, and `typing(reply_ctx)` (optional, default no-op
  async context manager). One implementation per chat platform lives in
  `pocketagent/platforms/` (`discord_platform.py`, `telegram_platform.py`,
  `slack_platform.py`).
- **`Agent`** / **`AgentSession`** (`core/agent.py`): `Agent.start_session(session_id,
  work_dir)` returns an `AgentSession` with `send(prompt, images, files)`,
  `events()` (async iterator of `Event`), `alive()`, `close()`. One implementation per
  agent backend lives in `pocketagent/agents/` (`claude_code.py`, `codex.py`, `tmux.py`).

Adding a new platform or agent means adding one module implementing one of these
interfaces — `core/engine.py` does not need to change.

Supporting pieces in `core/`:

- **`router.py`**: resolves `(agent_name, work_dir, platform_system_prompt)` for a
  channel. A platform has one `default_agent` and one optional `platform_system_prompt`
  (applied to every channel on that platform); per-channel-id `ChannelOverride`s can pin
  a different agent and/or a fixed workspace folder name. `platform_system_prompt` is
  passed to `Agent.start_session`, where it's combined with that agent's own
  `agent_system_prompt` (a constructor-time option set per agent in config, e.g.
  `[agents.claude_code]`). The claude_code backend joins the two and forwards them via
  `--append-system-prompt`; the codex backend joins them the same way but forwards them via
  `-c developer_instructions=<toml-string>` (its closest equivalent); the tmux backend has
  no generic way to apply either to an arbitrary terminal program and logs a warning instead.
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

### Codex agent backend (`agents/codex.py`)

Unlike claude_code's persistent stdin/stdout process, `codex exec` is one-shot: each
invocation runs a single turn to completion and exits, so this backend spawns a fresh
`codex exec --json ...` subprocess per `send()` instead of holding one open for the life
of the session. `--json` turns stdout into a JSON-Lines event stream
(`thread.started`/`item.completed`/`turn.completed`/`turn.failed`/`error`); stderr (where
human-readable progress goes) is drained and discarded so it can't fill its pipe and stall
the process. The `thread_id` from `thread.started` is this backend's session id, fed back
via `resume <thread_id>` on the next turn. `ask_for_approval` defaults to `"never"` since
codex exec has no stdio permission-prompt protocol to answer approval requests the way
Claude Code does. `translate_message()` mirrors claude_code's pure-function design (see
`tests/test_codex_protocol.py`); only `agent_message`/`reasoning` items have a
field-confirmed shape, so other item types (`command_execution`, `file_change`,
`mcp_tool_call`, `web_search`) are surfaced as generic `TOOL_USE` events carrying the
whole item as JSON.

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
threads/buttons yet — plain text messaging only.

Configured custom commands are also registered as real Discord slash commands (via
`discord.app_commands.CommandTree`), each with a single free-text `args` option.
Invoking one reconstructs the equivalent `/name args...` text and feeds it through the
same `CommandRegistry.expand()` path used for typed text commands, so behavior is
identical either way. Slash-command interactions skip the `@`-mention gating entirely
(invoking one is already an explicit action) but still go through `allow_list`.

### Telegram platform (`platforms/telegram_platform.py`)

Built on `python-telegram-bot`, run inside the existing asyncio loop via the manual
`Application.initialize()` / `.start()` / `updater.start_polling()` lifecycle rather than
the blocking `run_polling()` convenience wrapper. Private chats are always dispatched;
group/supergroup messages only dispatch when the bot's `@username` appears in the text
(stripped before handing off), unless the chat id is listed in `group_reply_all_chats`
(mirrors Discord's `group_reply_all_guilds`/`require_mention_channels`, renamed
`require_mention_chats` here). Configured custom commands are registered with Telegram via
`setMyCommands` purely for command-autocomplete in the client; unlike Discord, Telegram
already delivers `/name args` as ordinary message text, so it flows through the same
`CommandRegistry.expand()` path with no separate dispatch path needed. There's no native
typing-indicator context manager in `python-telegram-bot`, so `typing()` builds one that
calls `sendChatAction(typing)` on a loop (Telegram's indicator only lasts ~5s per call).

### Slack platform (`platforms/slack_platform.py`)

Built on `slack_bolt`'s async app, connected via Socket Mode (a persistent outbound
websocket, started with `AsyncSocketModeHandler.connect_async()`) rather than the Events
API's inbound HTTP webhook — like Discord's gateway and Telegram's polling, this needs no
public endpoint or reverse proxy, so it works behind NAT. Requires two tokens: a bot token
(`xoxb-...`) for posting/reading, and an app-level token (`xapp-...`, needs the
`connections:write` scope) for the Socket Mode connection itself. DMs are always
dispatched; public/private channel messages only dispatch when the bot is `@`-mentioned
(stripped before handing off), unless the channel id is listed in
`group_reply_all_channels` (mirrors Discord's `group_reply_all_guilds` /
`require_mention_channels`, renamed `require_mention_channels` here too since Slack calls
them channels already). Unlike Discord, Slack slash commands must be pre-registered in the
app's own configuration (Slack's UI) rather than dynamically at runtime, so configured
custom commands aren't registered as real Slack slash commands — typing `/name args` in a
channel where `/name` isn't a Slack-native slash command for any installed app arrives as
ordinary message text and flows through the same `CommandRegistry.expand()` path, mirroring
Telegram. Slack's Web API has no "bot is typing" indicator for Socket Mode (the old
RTM-only `typing` event was removed for bots), so `typing()` falls back to the base class's
no-op. Channel names and display names aren't included on the `message` event itself, so
they're fetched once per id via `conversations_info` / `users_info` and cached on the
platform instance.
