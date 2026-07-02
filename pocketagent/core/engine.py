"""Wires together: platform message -> commands -> routing -> agent -> reply."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .agent import Agent
from .commands import CommandRegistry, CustomCommand
from .platform import Platform
from .utils import format_duration, parse_relative_duration
from .router import ResolvedRoute, Router
from .schedule_requests import (
    SCHEDULE_TASK_INSTRUCTIONS,
    ScheduleRequestError,
    extract_list_task_requests,
    extract_remove_task_requests,
    extract_schedule_requests,
)
from .scheduled_tasks import ScheduledTask, append_scheduled_task, load_scheduled_tasks, remove_scheduled_task
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
    # Names of the built-in /scheduled and /unschedule commands registered
    # by _register_builtin_commands -- a user's own custom command of the
    # same name takes precedence (that check lives in
    # _register_builtin_commands itself).
    _LIST_SCHEDULED_COMMAND = "scheduled"
    _UNSCHEDULE_COMMAND = "unschedule"

    def __init__(
        self,
        agents: dict[str, Agent],
        routers: dict[str, Router],
        session_store: SessionStore,
        commands: CommandRegistry,
        scheduled_tasks_dir: Path | None = None,
    ) -> None:
        self.agents = agents
        self.routers = routers
        self.session_store = session_store
        self.commands = commands
        # Where to append scheduled_tasks.toml entries requested by an agent
        # via a ```schedule-task``` block in its reply -- see
        # core/schedule_requests.py. None disables the feature entirely (no
        # system-prompt instructions, no /scheduled or /unschedule commands,
        # and any schedule-task-family block found is reported back as an
        # error instead of being actioned).
        self._scheduled_tasks_dir = scheduled_tasks_dir
        if scheduled_tasks_dir is not None:
            self._register_builtin_commands()
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

    def _register_builtin_commands(self) -> None:
        """Register /scheduled and /unschedule as real CommandRegistry entries.

        Unlike the hardcoded-in-Engine approach this replaced, going through
        CommandRegistry means platforms that list `commands.all()` to
        register real slash commands / autocomplete (Discord, Telegram) pick
        these up automatically too. Skipped per-name if the user already
        defined their own command of that name in config -- theirs wins.
        """

        if self.commands.resolve(self._LIST_SCHEDULED_COMMAND) is None:
            self.commands.add(
                CustomCommand(
                    name=self._LIST_SCHEDULED_COMMAND,
                    builtin="list_scheduled_tasks",
                    description="List scheduled tasks for this conversation",
                )
            )
        if self.commands.resolve(self._UNSCHEDULE_COMMAND) is None:
            self.commands.add(
                CustomCommand(
                    name=self._UNSCHEDULE_COMMAND,
                    builtin="remove_scheduled_task",
                    description="Remove a scheduled task by id",
                )
            )

    async def _handle_locked(self, platform: Platform, msg: Message, route: ResolvedRoute) -> None:
        expanded = self.commands.expand(msg.content)
        if expanded is not None:
            cmd, expanded_text = expanded
            if cmd.builtin is not None:
                await self._handle_builtin_command(cmd, expanded_text, platform, msg)
                return
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

        platform_system_prompt = route.platform_system_prompt
        if self._scheduled_tasks_dir is not None:
            platform_system_prompt = (
                f"{platform_system_prompt}\n\n{SCHEDULE_TASK_INSTRUCTIONS}"
                if platform_system_prompt
                else SCHEDULE_TASK_INSTRUCTIONS
            )

        async with platform.typing(msg.reply_ctx):
            session = await self.session_store.get_or_create(
                msg.session_key,
                agent,
                str(route.work_dir),
                platform_system_prompt,
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
                    final_text = self._apply_schedule_requests(final_text, msg)
                    footer = _format_footer(event) if route.show_footer else ""
                    if footer:
                        final_text = f"{final_text}\n\n{footer}" if final_text else footer
                    if final_text:
                        await platform.reply(msg.reply_ctx, final_text)
                    self._maybe_mark_rate_limited_from_usage(route.agent_name, event)
                    return

    def _apply_schedule_requests(self, text: str, msg: Message) -> str:
        """Strip any schedule-task/list-scheduled-tasks/remove-schedule-task
        blocks out of `text` and act on them.

        platform/channel_id/user_id come from `msg` (the real incoming
        message), never from any block itself, so the agent can only ever
        add/list/remove tasks tied to the channel/user it's actually
        replying to, never another conversation's.
        """

        cleaned, add_requests = extract_schedule_requests(text)
        cleaned, list_count = extract_list_task_requests(cleaned)
        cleaned, remove_requests = extract_remove_task_requests(cleaned)

        if not add_requests and not list_count and not remove_requests:
            return text

        notes: list[str] = [
            *self._process_add_requests(add_requests, msg),
            *self._process_list_requests(list_count, msg),
            *self._process_remove_requests(remove_requests, msg),
        ]

        note_text = "\n".join(notes)
        return f"{cleaned}\n\n{note_text}" if cleaned else note_text

    def _process_add_requests(self, requests: list, msg: Message) -> list[str]:
        notes: list[str] = []
        for request in requests:
            if isinstance(request, ScheduleRequestError):
                notes.append(f"Couldn't schedule that: {request.detail}")
                continue
            if self._scheduled_tasks_dir is None:
                notes.append("Couldn't schedule that: scheduled tasks aren't configured on this server.")
                continue
            task = ScheduledTask(
                platform=msg.platform,
                channel_id=msg.channel_id,
                user_id=msg.user_id,
                prompt=request.prompt,
                cron=request.cron,
                timezone=request.timezone,
                interval_weeks=request.interval_weeks,
            )
            try:
                task_id = append_scheduled_task(self._scheduled_tasks_dir, task)
            except OSError:
                logger.exception("failed to append scheduled task")
                notes.append("Couldn't schedule that: failed to save it.")
                continue
            cadence = "" if request.interval_weeks == 1 else f" (every {request.interval_weeks} weeks)"
            when = f"'{request.cron}' {request.timezone}".strip()
            notes.append(f"Scheduled {when}{cadence} (id: {task_id}).")
        return notes

    def _process_list_requests(self, count: int, msg: Message) -> list[str]:
        if count == 0:
            return []
        return [self._list_scheduled_tasks_text(msg)] * count

    def _process_remove_requests(self, requests: list, msg: Message) -> list[str]:
        notes: list[str] = []
        for request in requests:
            if isinstance(request, ScheduleRequestError):
                notes.append(f"Couldn't remove that: {request.detail}")
                continue
            notes.append(self._remove_scheduled_task_text(msg, request.id))
        return notes

    def _list_scheduled_tasks_text(self, msg: Message) -> str:
        """Format the scheduled tasks belonging to msg's (platform, channel_id,
        user_id) -- shared by the ```list-scheduled-tasks``` block and the
        /scheduled command."""

        if self._scheduled_tasks_dir is None:
            return "Couldn't list scheduled tasks: scheduled tasks aren't configured on this server."
        try:
            tasks = load_scheduled_tasks(self._scheduled_tasks_dir)
        except Exception:
            logger.exception("failed to load scheduled tasks for listing")
            return "Couldn't list scheduled tasks: failed to read them."

        matching = [
            t
            for t in tasks
            if t.platform == msg.platform and t.channel_id == msg.channel_id and t.user_id == msg.user_id
        ]
        if not matching:
            return "No scheduled tasks for this conversation."
        lines = []
        for t in matching:
            when = f"'{t.cron}'{f' {t.timezone}' if t.timezone else ''}"
            cadence = f" (every {t.interval_weeks} weeks)" if t.interval_weeks != 1 else ""
            lines.append(f"- id: {t.id} -- {when}{cadence} -- {t.prompt}")
        return "Scheduled tasks for this conversation:\n" + "\n".join(lines)

    def _remove_scheduled_task_text(self, msg: Message, task_id: str) -> str:
        """Remove the entry `task_id` scoped to msg's (platform, channel_id,
        user_id) and report the outcome -- shared by the
        ```remove-schedule-task``` block and the /unschedule command."""

        if self._scheduled_tasks_dir is None:
            return "Couldn't remove that: scheduled tasks aren't configured on this server."
        try:
            removed = remove_scheduled_task(
                self._scheduled_tasks_dir, task_id, msg.platform, msg.channel_id, msg.user_id
            )
        except OSError:
            logger.exception("failed to remove scheduled task")
            return "Couldn't remove that: failed to update the file."
        if removed:
            return f"Removed scheduled task (id: {task_id})."
        return f"Couldn't find a scheduled task with id '{task_id}' in this conversation."

    async def _handle_builtin_command(
        self, cmd: CustomCommand, args_text: str, platform: Platform, msg: Message
    ) -> None:
        if cmd.builtin == "list_scheduled_tasks":
            await platform.reply(msg.reply_ctx, self._list_scheduled_tasks_text(msg))
            return
        if cmd.builtin == "remove_scheduled_task":
            args = args_text.split()
            if not args:
                await platform.reply(
                    msg.reply_ctx,
                    f"Usage: /{self._UNSCHEDULE_COMMAND} <id> -- see /{self._LIST_SCHEDULED_COMMAND} for ids.",
                )
                return
            await platform.reply(msg.reply_ctx, self._remove_scheduled_task_text(msg, args[0]))
            return
        raise AssertionError(f"unknown builtin command {cmd.builtin!r}")

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
