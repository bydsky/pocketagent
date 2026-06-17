"""Discord platform backed by discord.py.

DMs are always dispatched; guild channel messages only dispatch when the
bot is @mentioned (the mention is stripped before handing the message to
the engine). group_reply_all_guilds lifts the mention requirement for
specific guilds (or "*" for all); require_mention_channels re-imposes it
for specific channels even inside those guilds. No threads/buttons yet --
plain text messaging only.

Configured custom commands (see core/commands.py) are additionally
registered as real Discord slash commands, each with a single free-text
"args" option -- invoking one reconstructs the equivalent "/name args..."
text and feeds it through the exact same CommandRegistry.expand() path
used for typed text commands, so behavior (including {{1}}/{{N*}}/{{args}}
placeholders) is identical either way. Slash commands skip the @mention
gating that applies to plain messages, since invoking one is already an
explicit, unambiguous action.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import discord
from discord import app_commands

from ..core.commands import CommandRegistry
from ..core.platform import MessageHandler, Platform, allow_list, csv_contains
from ..core.textsplit import split_message
from ..core.types import FileAttachment, ImageAttachment, Message

logger = logging.getLogger(__name__)

MAX_DISCORD_LEN = 1900


async def _classify_attachments(
    attachments: list[discord.Attachment],
) -> tuple[list[ImageAttachment], list[FileAttachment]]:
    images: list[ImageAttachment] = []
    files: list[FileAttachment] = []
    for att in attachments:
        try:
            data = await att.read()
        except discord.HTTPException:
            logger.warning("discord: failed to download attachment %s", att.filename)
            continue
        content_type = (att.content_type or "").lower()
        is_image = content_type.startswith("image/") or (
            not content_type and att.width and att.height
        )
        if is_image:
            images.append(
                ImageAttachment(mime_type=content_type or "image/png", data=data, file_name=att.filename)
            )
        else:
            files.append(FileAttachment(mime_type=content_type, data=data, file_name=att.filename))
    return images, files


class DiscordPlatform(Platform):
    name = "discord"

    def __init__(
        self,
        token: str,
        allow_from: str = "",
        require_mention: bool = True,
        group_reply_all_guilds: str = "",
        require_mention_channels: str = "",
        commands: CommandRegistry | None = None,
    ) -> None:
        if not token:
            raise ValueError("discord: token is required")
        self.token = token
        self.allow_from = allow_from
        self.require_mention = require_mention
        self.group_reply_all_guilds = group_reply_all_guilds
        self.require_mention_channels = require_mention_channels
        self.commands = commands
        self._client: discord.Client | None = None
        self._gateway_task: asyncio.Task | None = None
        self._handler: MessageHandler | None = None
        self._synced = False

    async def start(self, handler: MessageHandler) -> None:
        self._handler = handler

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.dm_messages = True
        intents.guild_messages = True

        client = discord.Client(intents=intents)
        self._client = client
        tree = app_commands.CommandTree(client)
        self._register_slash_commands(tree)

        @client.event
        async def on_ready() -> None:
            logger.info("discord: connected as %s", client.user)
            if not self._synced:
                self._synced = True
                try:
                    synced = await tree.sync()
                    logger.info("discord: synced %d slash command(s)", len(synced))
                except discord.HTTPException:
                    logger.exception("discord: failed to sync slash commands")

        @client.event
        async def on_message(message: discord.Message) -> None:
            await self._on_discord_message(message)

        self._gateway_task = asyncio.create_task(client.start(self.token))
        await asyncio.wait_for(client.wait_until_ready(), timeout=30)

    def _register_slash_commands(self, tree: app_commands.CommandTree) -> None:
        if not self.commands:
            return
        for cmd in self.commands.all():
            callback = app_commands.describe(args="Arguments (optional)")(
                self._make_slash_callback(cmd.name)
            )
            tree.command(name=cmd.name, description=(cmd.description or cmd.name)[:100])(callback)

    def _make_slash_callback(self, name: str) -> Callable[[discord.Interaction, str], Awaitable[None]]:
        async def _callback(interaction: discord.Interaction, args: str = "") -> None:
            await self._on_slash_command(interaction, name, args)

        _callback.__name__ = f"slash_{name}"
        return _callback

    async def _on_slash_command(self, interaction: discord.Interaction, name: str, args: str) -> None:
        if not allow_list(self.allow_from, str(interaction.user.id)):
            logger.debug("discord: slash command from unauthorized user %s", interaction.user.id)
            await interaction.response.send_message("You're not authorized to use this bot.", ephemeral=True)
            return

        await interaction.response.defer()

        channel_name = getattr(interaction.channel, "name", "") or ""
        msg = Message(
            session_key=f"discord:{interaction.channel_id}:{interaction.user.id}",
            channel_key=str(interaction.channel_id),
            platform="discord",
            channel_id=str(interaction.channel_id),
            user_id=str(interaction.user.id),
            user_name=str(interaction.user.display_name),
            chat_name=channel_name,
            content=f"/{name} {args}".rstrip(),
            reply_ctx=interaction,
        )
        assert self._handler is not None
        await self._handler(self, msg)

    async def _on_discord_message(self, message: discord.Message) -> None:
        client = self._client
        if client is None or client.user is None:
            return
        if message.author.bot or message.author.id == client.user.id:
            return
        if not allow_list(self.allow_from, str(message.author.id)):
            logger.debug("discord: message from unauthorized user %s", message.author.id)
            return

        content = message.content
        is_guild = message.guild is not None
        if is_guild:
            needs_mention = self.require_mention
            if csv_contains(self.group_reply_all_guilds, str(message.guild.id)):
                needs_mention = False
            if csv_contains(self.require_mention_channels, str(message.channel.id)):
                needs_mention = True

            mentioned = client.user in message.mentions
            if needs_mention and not mentioned:
                return
            content = content.replace(f"<@{client.user.id}>", "").replace(
                f"<@!{client.user.id}>", ""
            ).strip()

        if not content and not message.attachments:
            return

        images, files = await _classify_attachments(message.attachments)
        channel_name = getattr(message.channel, "name", "") or ""

        msg = Message(
            session_key=f"discord:{message.channel.id}:{message.author.id}",
            channel_key=str(message.channel.id),
            platform="discord",
            channel_id=str(message.channel.id),
            user_id=str(message.author.id),
            user_name=str(message.author.display_name),
            chat_name=channel_name,
            content=content,
            images=images,
            files=files,
            reply_ctx=message,
        )
        assert self._handler is not None
        await self._handler(self, msg)

    async def reply(self, reply_ctx, content: str) -> None:
        if isinstance(reply_ctx, discord.Interaction):
            for chunk in split_message(content, MAX_DISCORD_LEN):
                await reply_ctx.followup.send(chunk)
            return
        message: discord.Message = reply_ctx
        for chunk in split_message(content, MAX_DISCORD_LEN):
            await message.reply(chunk)

    async def send(self, reply_ctx, content: str) -> None:
        if isinstance(reply_ctx, discord.Interaction):
            for chunk in split_message(content, MAX_DISCORD_LEN):
                await reply_ctx.followup.send(chunk)
            return
        message: discord.Message = reply_ctx
        for chunk in split_message(content, MAX_DISCORD_LEN):
            await message.channel.send(chunk)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()
        if self._gateway_task is not None:
            self._gateway_task.cancel()
            try:
                await self._gateway_task
            except (asyncio.CancelledError, Exception):
                pass
