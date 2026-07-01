"""Wires together: platform message -> commands -> routing -> agent -> reply."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from .agent import Agent
from .commands import CommandRegistry
from .platform import Platform
from .utils import format_duration, parse_relative_duration
from .router import ResolvedRoute, Router
from .scheduler import OneShotScheduler
from .session_store import SessionStore
from .types import Event, EventType, Message

logger = logging.getLogger(__name__)


def _format_footer(event: Event) -> str:
    """Build a "`model · effort · N tokens · ctx:%  5h:%  7d:%  $cost`" inline-code
    footer from whatever the agent reported.

    event.model is already display-formatted by whichever agent backend set it
    (see claude_code._format_model_name) -- the engine stays agent-agnostic and
    just passes it through. Only claude_code's RESULT event currently carries
    model/cost_usd/context_used_pct/rate_limit_*_pct (see its module docstring);
    codex's RESULT event has none of these, so it just gets a token count, and a
    fully empty event (e.g. in tests) gets no footer at all.
    """

    parts = []
    if event.model:
        parts.append(event.model)
    if event.effort:
        parts.append(event.effort)
    total_tokens = event.input_tokens + event.output_tokens
    if total_tokens:
        parts.append(f"{total_tokens} tokens")
    if event.context_used_pct is not None:
        parts.append(f"ctx:{event.context_used_pct}%")
    if event.rate_limit_5h_pct is not None:
        reset = f"({event.rate_limit_5h_reset_in})" if event.rate_limit_5h_reset_in else ""
        parts.append(f"5h:{event.rate_limit_5h_pct}%{reset}")
    if event.rate_limit_7d_pct is not None:
        reset = f"({event.rate_limit_7d_reset_in})" if event.rate_limit_7d_reset_in else ""
        parts.append(f"7d:{event.rate_limit_7d_pct}%{reset}")
    if event.cost_usd is not None:
        parts.append(f"${event.cost_usd:.4f}")
    return f"`{' · '.join(parts)}`" if parts else ""


def _format_eta(retry_at: datetime) -> str:
    return format_duration(max(timedelta(0), retry_at - datetime.now(retry_at.tzinfo)))


class Engine:
    def __init__(
        self,
        agents: dict[str, Agent],
        routers: dict[str, Router],
        session_store: SessionStore,
        commands: CommandRegistry,
    ) -> None:
        self.agents = agents
        self.routers = routers
        self.session_store = session_store
        self.commands = commands
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Usage-limit backlog: keyed by agent_name, since the underlying limit
        # is account-wide (shared by every channel routed to that agent), not
        # per-channel. A timer present in _rate_limit_timers means that agent
        # is currently exhausted; its run_at is the next retry instant. Held
        # only in memory -- a restart drops anything queued, same as live
        # (non-persisted) AgentSessions.
        self._rate_limit_timers: dict[str, OneShotScheduler] = {}
        self._rate_limit_backlog: dict[str, list[tuple[Platform, Message]]] = {}

    async def on_message(self, platform: Platform, msg: Message) -> None:
        try:
            await self._handle(platform, msg)
        except Exception:
            logger.exception("error handling message session_key=%s", msg.session_key)
            await platform.reply(msg.reply_ctx, "Sorry, something went wrong handling that message.")

    async def _handle(self, platform: Platform, msg: Message) -> None:
        router = self.routers.get(msg.platform)
        if router is None:
            await platform.reply(
                msg.reply_ctx, f"No routing configured for platform '{msg.platform}'."
            )
            return
        route = router.resolve(msg.channel_key or msg.channel_id, msg.chat_name)

        # Serialize per session_key: a second message arriving while the agent
        # is still working a prior one must queue rather than race it (the
        # claude_code backend speaks one stdin/stdout turn at a time, and a
        # concurrent send() would corrupt that stream).
        lock = self._session_locks.setdefault(msg.session_key, asyncio.Lock())
        if lock.locked():
            await platform.send(
                msg.reply_ctx,
                "Still working on a previous message in this conversation -- "
                "yours is queued and will run next.",
            )

        async with lock:
            await self._handle_locked(platform, msg, route)

    async def _handle_locked(self, platform: Platform, msg: Message, route: ResolvedRoute) -> None:
        expanded = self.commands.expand(msg.content)
        if expanded is not None:
            cmd, expanded_text = expanded
            if cmd.exec is not None:
                async with platform.typing(msg.reply_ctx):
                    output = await self._run_exec(expanded_text, str(route.work_dir))
                await platform.reply(msg.reply_ctx, output)
                return
            msg.content = expanded_text

        agent = self.agents.get(route.agent_name)
        if agent is None:
            await platform.reply(
                msg.reply_ctx, f"No agent configured named '{route.agent_name}'."
            )
            return

        timer = self._rate_limit_timers.get(route.agent_name)
        if timer is not None:
            await self._queue_for_retry(route.agent_name, platform, msg, timer.run_at)
            return

        async with platform.typing(msg.reply_ctx):
            session = await self.session_store.get_or_create(
                msg.session_key,
                agent,
                str(route.work_dir),
                route.platform_system_prompt,
                route.show_footer,
            )
            prompt = msg.content
            if msg.quoted_content:
                prompt = f"> {msg.quoted_content}\n\n{msg.content}"
            await session.send(prompt, msg.images, msg.files)

            text_parts: list[str] = []
            async for event in session.events():
                if event.session_id:
                    self.session_store.set_resume_id(msg.session_key, event.session_id)
                if event.type == EventType.TEXT:
                    text_parts.append(event.content)
                if event.done:
                    if event.type == EventType.ERROR:
                        error_text = event.error or "agent reported an error"
                        if event.rate_limit_retry_at is not None:
                            self._mark_rate_limited(route.agent_name, event.rate_limit_retry_at)
                            await self._queue_for_retry(
                                route.agent_name, platform, msg, event.rate_limit_retry_at
                            )
                            return
                        await platform.reply(msg.reply_ctx, f"Error: {error_text}")
                        return
                    final_text = "".join(text_parts) or event.content
                    footer = _format_footer(event) if route.show_footer else ""
                    if footer:
                        final_text = f"{final_text}\n\n{footer}" if final_text else footer
                    if final_text:
                        await platform.reply(msg.reply_ctx, final_text)
                    self._maybe_mark_rate_limited_from_usage(route.agent_name, event)
                    return

    async def _run_exec(self, command: str, work_dir: str) -> str:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace").strip()
        return output or f"(command exited with code {proc.returncode}, no output)"

    async def _queue_for_retry(
        self, agent_name: str, platform: Platform, msg: Message, retry_at: datetime
    ) -> None:
        self._rate_limit_backlog.setdefault(agent_name, []).append((platform, msg))
        await platform.reply(
            msg.reply_ctx,
            f"Usage limit reached for this agent -- your message has been queued "
            f"and will run automatically in about {_format_eta(retry_at)}.",
        )

    def _mark_rate_limited(self, agent_name: str, retry_at: datetime) -> None:
        timer = self._rate_limit_timers.get(agent_name)
        if timer is not None:
            timer.reschedule(retry_at)
            return
        timer = OneShotScheduler(retry_at, lambda: self._flush_backlog(agent_name))
        self._rate_limit_timers[agent_name] = timer
        timer.start()

    def _maybe_mark_rate_limited_from_usage(self, agent_name: str, event: Event) -> None:
        """Proactive signal: a successful turn's footer data already reports
        5h/7d usage at/over 100% -- prime the backlog now so the *next*
        message (on any channel routed to this agent) queues immediately
        instead of burning a call already known to be denied."""

        for pct, reset_in in (
            (event.rate_limit_5h_pct, event.rate_limit_5h_reset_in),
            (event.rate_limit_7d_pct, event.rate_limit_7d_reset_in),
        ):
            if pct is None or pct < 100 or not reset_in:
                continue
            delta = parse_relative_duration(reset_in)
            if delta is not None:
                self._mark_rate_limited(agent_name, datetime.now(timezone.utc) + delta)

    async def _flush_backlog(self, agent_name: str) -> None:
        self._rate_limit_timers.pop(agent_name, None)
        backlog = self._rate_limit_backlog.pop(agent_name, [])
        logger.info(
            "usage limit reset for agent=%s, replaying %d queued message(s)", agent_name, len(backlog)
        )
        for platform, msg in backlog:
            await self.on_message(platform, msg)

    async def clear_sessions(self, predicate: Callable[[str], bool]) -> None:
        """Reset every session_key matching predicate -- used by the daily-reset scheduler."""

        await self.session_store.clear_matching(predicate)
        logger.info("daily reset: cleared matching sessions")

    async def clear_all_sessions(self) -> None:
        """Reset every channel's conversation -- used by the daily-reset scheduler."""

        await self.clear_sessions(lambda _: True)

    async def shutdown(self) -> None:
        for timer in self._rate_limit_timers.values():
            await timer.stop()
        await self.session_store.close_all()
        for agent in self.agents.values():
            await agent.stop()
