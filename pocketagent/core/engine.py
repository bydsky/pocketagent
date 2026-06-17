"""Wires together: platform message -> commands -> routing -> agent -> reply."""

from __future__ import annotations

import asyncio
import logging

from .agent import Agent
from .commands import CommandRegistry
from .platform import Platform
from .router import Router
from .session_store import SessionStore
from .types import EventType, Message

logger = logging.getLogger(__name__)


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
