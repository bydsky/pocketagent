"""Wires together: platform message -> commands -> routing -> agent -> reply."""

from __future__ import annotations

import asyncio
import logging

from .agent import Agent
from .commands import CommandRegistry
from .platform import Platform
from .router import ResolvedRoute, Router
from .session_store import SessionStore
from .types import Event, EventType, Message

logger = logging.getLogger(__name__)


def _format_footer(event: Event) -> str:
    """Build a "· model · N tokens · ctx:%  5h:%  7d:%  $cost" footer from whatever
    the agent reported.

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
    total_tokens = event.input_tokens + event.output_tokens
    if total_tokens:
        parts.append(f"{total_tokens} tokens")
    if event.context_used_pct is not None:
        parts.append(f"ctx:{event.context_used_pct}%")
    if event.rate_limit_5h_pct is not None:
        parts.append(f"5h:{event.rate_limit_5h_pct}%")
    if event.rate_limit_7d_pct is not None:
        parts.append(f"7d:{event.rate_limit_7d_pct}%")
    if event.cost_usd is not None:
        parts.append(f"${event.cost_usd:.4f}")
    return f"· {' · '.join(parts)}" if parts else ""


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

        async with platform.typing(msg.reply_ctx):
            session = await self.session_store.get_or_create(
                msg.session_key, agent, str(route.work_dir), route.platform_system_prompt
            )
            await session.send(msg.content, msg.images, msg.files)

            text_parts: list[str] = []
            async for event in session.events():
                if event.session_id:
                    self.session_store.set_resume_id(msg.session_key, event.session_id)
                if event.type == EventType.TEXT:
                    text_parts.append(event.content)
                if event.done:
                    if event.type == EventType.ERROR:
                        error_text = event.error or "agent reported an error"
                        await platform.reply(msg.reply_ctx, f"Error: {error_text}")
                        return
                    final_text = "".join(text_parts) or event.content
                    footer = _format_footer(event) if route.show_footer else ""
                    if footer:
                        final_text = f"{final_text}\n\n{footer}" if final_text else footer
                    if final_text:
                        await platform.reply(msg.reply_ctx, final_text)
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

    async def shutdown(self) -> None:
        await self.session_store.close_all()
        for agent in self.agents.values():
            await agent.stop()
