"""Slack platform backed by slack_bolt's async Socket Mode app.

Socket Mode (a persistent outbound websocket, no public HTTP endpoint
required) is used instead of Slack's Events API webhook so this runs the
same way Discord's gateway and Telegram's polling do -- no inbound port, no
reverse proxy, works fine behind NAT (e.g. a home Raspberry Pi). It needs
two tokens: a bot token (xoxb-..., for posting messages and reading files)
and an app-level token (xapp-..., for the Socket Mode connection itself --
requires the "connections:write" scope on the app).

DMs are always dispatched; public/private channel messages only dispatch
when the bot is @mentioned (the mention is stripped before handing the
message to the engine), unless the channel is listed in
group_reply_all_channels (or it's "*" for every channel), which lifts the
mention requirement. require_mention_channels re-imposes it for specific
channel ids even inside those.

Unlike Discord, Slack slash commands must be pre-registered in the app's
own configuration (Slack's UI), not dynamically at runtime -- so configured
custom commands (core/commands.py) are not registered as real Slack slash
commands here. Typing "/name args" in a channel where "/name" isn't a
registered Slack-native slash command for any installed app arrives as
ordinary message text, which flows through the same CommandRegistry.expand()
path as any other text, mirroring how Telegram handles it.

Slack's Web API has no "bot is typing" indicator for the Events API/Socket
Mode (the old RTM-only `typing` event was removed for bots), so typing()
falls back to the no-op default from the base class.
"""

from __future__ import annotations

import logging

import aiohttp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from ..core.commands import CommandRegistry
from ..core.platform import MessageHandler, Platform, allow_list, csv_contains
from ..core.textsplit import split_message
from ..core.types import FileAttachment, ImageAttachment, Message

logger = logging.getLogger(__name__)

MAX_SLACK_LEN = 3500
MAX_QUOTED_LEN = 500


async def _download_file(session: aiohttp.ClientSession, token: str, url: str) -> bytes | None:
    try:
        async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
            if resp.status != 200:
                return None
            return await resp.read()
    except aiohttp.ClientError:
        return None


async def _classify_attachments(
    token: str, files: list[dict]
) -> tuple[list[ImageAttachment], list[FileAttachment]]:
    images: list[ImageAttachment] = []
    file_attachments: list[FileAttachment] = []
    if not files:
        return images, file_attachments

    async with aiohttp.ClientSession() as session:
        for f in files:
            url = f.get("url_private_download") or f.get("url_private")
            if not url:
                continue
            data = await _download_file(session, token, url)
            if data is None:
                logger.warning("slack: failed to download file %s", f.get("name", ""))
                continue
            mime_type = (f.get("mimetype") or "").lower()
            name = f.get("name") or f.get("id", "")
            if mime_type.startswith("image/"):
                images.append(ImageAttachment(mime_type=mime_type, data=data, file_name=name))
            else:
                file_attachments.append(FileAttachment(mime_type=mime_type, data=data, file_name=name))
    return images, file_attachments


class SlackPlatform(Platform):
    name = "slack"

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        allow_from: str = "",
        group_reply_all_channels: str = "",
        require_mention_channels: str = "",
        commands: CommandRegistry | None = None,
    ) -> None:
        if not bot_token:
            raise ValueError("slack: bot_token is required")
        if not app_token:
            raise ValueError("slack: app_token is required")
        self.bot_token = bot_token
        self.app_token = app_token
        self.allow_from = allow_from
        self.group_reply_all_channels = group_reply_all_channels
        self.require_mention_channels = require_mention_channels
        self.commands = commands
        self._app: AsyncApp | None = None
        self._socket_handler: AsyncSocketModeHandler | None = None
        self._handler: MessageHandler | None = None
        self._bot_user_id: str = ""
        self._channel_name_cache: dict[str, str] = {}
        self._user_name_cache: dict[str, str] = {}

    async def start(self, handler: MessageHandler) -> None:
        self._handler = handler

        app = AsyncApp(token=self.bot_token)
        self._app = app
        app.event("message")(self._on_message)

        auth = await app.client.auth_test()
        self._bot_user_id = auth.get("user_id", "")

        self._socket_handler = AsyncSocketModeHandler(app, self.app_token)
        await self._socket_handler.connect_async()
        logger.info("slack: connected as %s", self._bot_user_id)

    async def _channel_name(self, client, channel_id: str) -> str:
        if channel_id in self._channel_name_cache:
            return self._channel_name_cache[channel_id]
        try:
            info = await client.conversations_info(channel=channel_id)
            name = (info.get("channel") or {}).get("name", "") or ""
        except SlackApiError:
            name = ""
        self._channel_name_cache[channel_id] = name
        return name

    async def _user_name(self, client, user_id: str) -> str:
        if user_id in self._user_name_cache:
            return self._user_name_cache[user_id]
        try:
            info = await client.users_info(user=user_id)
            profile = (info.get("user") or {}).get("profile") or {}
            name = profile.get("display_name") or profile.get("real_name") or user_id
        except SlackApiError:
            name = user_id
        self._user_name_cache[user_id] = name
        return name

    async def _on_message(self, event: dict, client) -> None:
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return
        user_id = event.get("user", "")
        if not user_id or user_id == self._bot_user_id:
            return
        if not allow_list(self.allow_from, user_id):
            logger.debug("slack: message from unauthorized user %s", user_id)
            return

        content = event.get("text", "") or ""
        channel_id = event.get("channel", "")
        channel_type = event.get("channel_type", "")
        is_channel = channel_type in ("channel", "group")

        if is_channel:
            needs_mention = not csv_contains(self.group_reply_all_channels, channel_id)
            if csv_contains(self.require_mention_channels, channel_id):
                needs_mention = True

            mention = f"<@{self._bot_user_id}>"
            mentioned = bool(self._bot_user_id) and mention in content
            if needs_mention and not mentioned:
                return
            if mentioned:
                content = content.replace(mention, "").strip()

        files = event.get("files") or []
        if not content and not files:
            return

        images, file_attachments = await _classify_attachments(self.bot_token, files)
        chat_name = await self._channel_name(client, channel_id) if is_channel else ""
        user_name = await self._user_name(client, user_id)
        quoted_content = await self._extract_quoted(client, event, channel_id)

        msg = Message(
            session_key=f"slack:{channel_id}:{user_id}",
            channel_key=channel_id,
            platform="slack",
            channel_id=channel_id,
            user_id=user_id,
            user_name=user_name,
            chat_name=chat_name,
            content=content,
            images=images,
            files=file_attachments,
            reply_ctx=event,
            quoted_content=quoted_content,
        )
        assert self._handler is not None
        await self._handler(self, msg)

    async def _extract_quoted(self, client, event: dict, channel_id: str) -> str:
        thread_ts = event.get("thread_ts")
        ts = event.get("ts")
        if not thread_ts or thread_ts == ts:
            return ""
        try:
            result = await client.conversations_history(
                channel=channel_id, latest=thread_ts, limit=1, inclusive=True
            )
            messages = (result.get("messages") or [])
            if messages:
                return (messages[0].get("text") or "")[:MAX_QUOTED_LEN]
        except SlackApiError:
            pass
        return ""

    async def reply(self, reply_ctx, content: str) -> None:
        await self._post(reply_ctx, content)

    async def send(self, reply_ctx, content: str) -> None:
        await self._post(reply_ctx, content)

    async def _post(self, reply_ctx: dict, content: str) -> None:
        assert self._app is not None
        channel_id = reply_ctx.get("channel", "")
        for chunk in split_message(content, MAX_SLACK_LEN):
            await self._app.client.chat_postMessage(channel=channel_id, text=chunk)

    async def stop(self) -> None:
        if self._socket_handler is not None:
            await self._socket_handler.disconnect_async()
            await self._socket_handler.close_async()
