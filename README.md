# pocketagent

Connects AI coding agents (Claude Code CLI, with Gemini CLI / tmux planned)
to chat platforms (Discord, with Slack / Telegram planned) so you can drive a
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

Requires the `claude` CLI to be installed and authenticated.

## Project layout

```
pocketagent/
  core/            platform-agnostic abstractions: Platform, Agent, Engine,
                   routing, workspaces, commands, session persistence
  platforms/       one module per chat platform (discord_platform.py)
  agents/          one module per agent backend (claude_code.py)
```

Adding a new platform or agent means adding one new module implementing the
`Platform` or `Agent`/`AgentSession` interface in `core/` — no changes to
`core/engine.py` required.

## Tests

```bash
pip install -e ".[dev]"
pytest
```
