# pocketagent

Connects AI coding agents (Claude Code, Codex, tmux, with Gemini CLI planned)
to chat platforms (Discord, Telegram, with Slack planned) so you can drive a
coding agent from a chat app.

## How it works

- Each platform has a **default agent** plus optional **per-channel-id
  overrides** (different agent and/or workspace per channel).
- Every channel gets its own **workspace folder** under the platform's
  `base_dir`, named after the channel (or a config override). Folder
  bindings persist in `.pocketagent-bindings.json` so renaming a channel
  doesn't orphan its history.
- **Custom commands** are config-defined prompt templates or shell
  commands. `/deploy api prod` expands a template like
  `"Deploy {{1}} to the {{2:staging}} environment..."` into a normal prompt
  sent to the agent. See `config.example.toml`.
- Conversation continuity survives restarts: the agent's own session id is
  persisted per channel/user and passed back via `--resume`.

## Setup

```bash
pip install -e .
cp config.example.toml pocketagent.toml
# edit pocketagent.toml: Discord bot token, base_dir, etc.
pocketagent run -c pocketagent.toml
```

### Discord bot setup

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and
   click **New Application**.
2. Under **Bot**, click **Reset Token** to generate a bot token, and save it — this goes
   in `[platforms.discord].token` in `pocketagent.toml`.
3. On the same **Bot** page, enable the **Message Content Intent** under
   "Privileged Gateway Intents" (required — pocketagent reads message text).
4. Under **OAuth2 > URL Generator**, select the `bot` scope and the
   `Send Messages` / `Read Message History` permissions, then open the
   generated URL to invite the bot to your server.
5. In `pocketagent.toml`, set `[platforms.discord].token` to the bot token from step 2,
   and adjust `base_dir`/`default_agent`/`allow_from` as needed.
6. DM the bot directly, or `@mention` it in a server channel it's in (guild channels
   require a mention by default; see `group_reply_all_guilds` in
   `config.example.toml` to relax that).

### Claude Code agent setup

The default `claude_code` agent backend drives the `claude` CLI as a subprocess, so it
must be installed and authenticated on the machine running pocketagent, separately from
any pocketagent config:

```bash
npm install -g @anthropic-ai/claude-code
claude    # run once interactively to log in / authenticate
```

`[agents.claude_code].command` in `pocketagent.toml` defaults to `"claude"` (resolved
via `PATH`); set it to a full path if the binary isn't on `PATH` for the user/service
running pocketagent.

## Project layout

```
pocketagent/
  core/            platform-agnostic abstractions: Platform, Agent, Engine,
                   routing, workspaces, commands, session persistence
  platforms/       one module per chat platform (discord_platform.py,
                   telegram_platform.py)
  agents/          one module per agent backend (claude_code.py, codex.py,
                   tmux.py)
```

Adding a new platform or agent means adding one new module implementing the
`Platform` or `Agent`/`AgentSession` interface in `core/` — no changes to
`core/engine.py` required.

## Tests

```bash
pip install -e ".[dev]"
pytest
```
