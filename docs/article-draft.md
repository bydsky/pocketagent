<!-- Draft — for dev.to / Show HN / blog. Swap in your own screenshots/GIF before
publishing; comparison claims are based on each project's public README as of
writing, not hands-on testing. -->

# One Discord server, four coding agents, zero mixed context

pocketagent bridges Discord, Telegram, and Slack to Claude Code, Codex, and any
terminal-based CLI agent — so you can drive a coding session from your phone,
and the agent can schedule its own follow-ups without you asking twice.

*~6 min read · self-hosted, open source, Python*

---

I kept having the same problem: Claude Code would be in the middle of
something useful on my desktop, and I'd leave the house. Or I'd think of
something at 11pm I wanted a coding agent to check on tomorrow morning, and by
the time I sat back down at a terminal I'd forgotten. So I built
**pocketagent** — a small bridge that sits between a chat platform and an
agent CLI, so a Discord DM, a Telegram message, or a Slack thread *is* the
terminal.

## 01 — What it actually does

Each platform gets a **default agent** plus optional per-channel overrides —
declared once in config, not switched at runtime. That's the difference
between "type `/model` every session" and "this channel has always been the
finance channel." One Discord server, four channels, four completely
different jobs:

```toml
# #coding -- the default agent, its own workspace
[platforms.discord.channels."123456789012345678"]
workspace = "product-backend"

# #shell -- raw ops, via a persistent tmux pane instead of a wrapped agent
[platforms.discord.channels."234567890123456789"]
agent = "tmux"
workspace = "infra"

# #finance -- a differently-configured agent entirely: bigger model, its own brief
[platforms.discord.channels."345678901234567890"]
agent = "finance_analyst"
workspace = "quarterly-models"

# #general -- idle chat, cheap model, no expectation it touches real work
[platforms.discord.channels."456789012345678901"]
agent = "casual_chat"
workspace = "scratch"

[agents.finance_analyst]
type = "claude_code"
model = "claude-opus-4-8"
agent_system_prompt = "You are a meticulous financial analyst. Cite sources, show your work."
```

`agent` here isn't just a workspace switch — it's a fully separate configured
backend: its own model, its own permission mode, its own system prompt. The
finance channel can run on a bigger model with a strict, source-citing brief
while `#general` runs cheap and casual, and nobody in either channel has to
know or care that the other one exists. Every channel also gets its own
workspace directory, and conversation continuity survives restarts — the
agent's own session id is persisted and passed back via `--resume`, so picking
a conversation back up tomorrow doesn't mean starting from zero context.

## 02 — A few smaller things round this out

**Reminders and schedules:** the agent can manage its own follow-ups. Say
"remind me every Thursday at 7pm to check the deploy," and it writes that
schedule for itself, mid-reply, as a small structured block:

````
```schedule-task
cron = "0 19 * * 4"
prompt = "Check on the build and report status."
```
````

pocketagent strips that block out before you ever see it and replaces it with
a plain confirmation — `Scheduled '0 19 * * 4' (id: a1b2c3d4).` A later "what
do I have scheduled?" or "cancel that" works the same way, and it's scoped so
an agent can only ever touch tasks tied to the exact channel/user it's
replying to — never another conversation's. One-off reminders work the same
way with a timestamp instead of a cron expression, and they clean up after
themselves — including a stale entry whose time already passed while the
process happened to be down, which gets quietly removed instead of firing a
reminder hours late.

**Daily reset:** conversations accumulate context forever by default, which
is usually right and occasionally means a channel is quietly dragging three
weeks of unrelated history into every reply — an optional `[daily_reset]`
cron wipes matching sessions on a schedule, and any channel can override it or
opt out entirely.

```toml
[daily_reset]
cron = "0 4 * * *"
timezone = "America/New_York"
```

**Usage-limit backlog:** Claude Code's own 5h/7d usage limits are real, and
hitting one mid-conversation used to just mean an error message. Now
pocketagent notices the denial, stops sending to that agent, queues incoming
messages — across every channel routed to it, not just the one that tripped
the limit — and automatically replays them in order the moment the limit
resets. **A footer, if you want it:** turn on `show_footer` per platform or
per channel and every reply gets a quiet one-liner appended —
`claude-opus-4-8 · high · 1,842 tokens · ctx:38% · 5h:22%(3h12m) · $0.1364` —
off by default, on for the channel where you actually want to watch the clock
on your usage limit.

## 03 — How it compares

There are several bridges doing some version of this already, each supporting
multiple agents and at least one chat platform with their own cron/job
systems. I'd rather be specific than claim a generic win, so here's what I
actually found reading their docs:

| Capability | pocketagent | Similar bridges |
|---|---|---|
| Agent schedules its own tasks | Yes — via reply blocks | Not found — commands are user-typed only |
| One-shot reminder, self-cleaning | Yes, incl. missed-window cleanup | One-shot with no cleanup noted, or recurring-only |
| Declarative per-channel agent/workspace | Yes — set once in config | Runtime commands only (e.g. `/model`, `/dir`, `/repo`) |
| Chat platforms supported | 3 (Discord, Telegram, Slack) | Some cover a dozen or more |
| Agent CLIs supported | 3, one of which wraps any TUI | Some cover 10+ via a shared protocol |

> The honest pitch isn't "more platforms" or "more agents" — it's the only one
> I found where the agent manages its own schedule, not just you.

## 04 — Try it

```bash
pip install -e .
cp config.example.toml pocketagent.toml
# edit pocketagent.toml: bot token, base_dir, etc.
pocketagent run -c pocketagent.toml
```

It's a straight Python process, no hosted service, no telemetry — your
tokens and your conversations stay on whatever machine you run it on.

> **Where it's not the right fit (yet).** The platform list — Discord,
> Telegram, Slack — is short because that's what I personally use, not
> because adding a fourth is hard: a platform is one module implementing one
> interface (`start`/`reply`/`send`/`stop`), and the engine doesn't change.
> **Contributions adding Feishu, LINE, WeChat Work, or anything else are
> welcome.** If instead you want to hop between a dozen ad hoc projects by
> typing `/dir` constantly, that's a genuinely different shape of tool than
> pocketagent's declarative one, and a runtime-switching bridge is probably
> still the better fit for that today.

---

Source, setup docs, and the full config reference:
**[github.com/bydsky/pocketagent](https://github.com/bydsky/pocketagent)**
