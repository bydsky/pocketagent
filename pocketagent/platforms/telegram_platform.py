"""Telegram platform backed by python-telegram-bot.

Private chats (DMs) are always dispatched; group/supergroup messages only
dispatch when the bot's @username is mentioned in the text (the mention is
stripped before handing the message to the engine), unless the chat is
listed in group_reply_all_chats (or group_reply_all_chats is "*" for every
chat), which lifts the mention requirement. require_mention_chats
re-imposes it for specific chat ids even inside those. Channel posts and
edited messages are ignored -- only ordinary chat messages are handled.

Configured custom commands (see core/commands.py) are additionally
registered with Telegram via setMyCommands purely so they show up in the
client's command-autocomplete UI; Telegram already delivers "/name args"
text as a normal message, which the engine routes through
CommandRegistry.expand() the same as any other text, so no special dispatch
path is needed for them the way Discord's slash commands require one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from telegram import BotCommand, Message, PhotoSize, Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from ..core.commands import CommandRegistry
from ..core.platform import MessageHandler as PocketMessageHandler
from ..core.platform import Platform, allow_list, csv_contains
from ..core.textsplit import split_message
from ..core.types import FileAttachment, ImageAttachment, Message as PocketMessage

logger = logging.getLogger(__name__)

MAX_TELEGRAM_LEN = 4000
TYPING_REFRESH_SECONDS = 4


async def _classify_attachments(
    message: Message,
) -> tuple[list[ImageAttachment], list[FileAttachment]]:
    images: list[ImageAttachment] = []
    files: list[FileAttachment] = []

    photo: PhotoSize | None = message.photo[-1] if message.photo else None
    if photo is not None:
        tg_file = await photo.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        images.append(ImageAttachment(mime_type="image/jpeg", data=data, file_name=f"{photo.file_unique_id}.jpg"))

    if message.document is not None:
        doc = message.document
        tg_file = await doc.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        content_type = (doc.mime_type or "").lower()
        att_name = doc.file_name or f"{doc.file_unique_id}"
        if content_type.startswith("image/"):
            images.append(ImageAttachment(mime_type=content_type, data=data, file_name=att_name))
        else:
            files.append(FileAttachment(mime_type=content_type, data=data, file_name=att_name))

    return images, files


class TelegramPlatform(Platform):
    name = "telegram"

    def __init__(
        self,
        token: str,
        allow_from: str = "",
        group_reply_all_chats: str = "",
        require_mention_chats: str = "",
        commands: CommandRegistry | None = None,
    ) -> None:
        if not token:
            raise ValueError("telegram: token is required")
        self.token = token
        self.allow_from = allow_from
        self.group_reply_all_chats = group_reply_all_chats
        self.require_mention_chats = require_mention_chats
        self.commands = commands
        self._app: Application | None = None
        self._handler: PocketMessageHandler | None = None
        self._bot_username: str = ""

    async def start(self, handler: PocketMessageHandler) -> None:
        self._handler = handler

        app = Application.builder().token(self.token).build()
        self._app = app
        app.add_handler(MessageHandler(filters.ALL, self._on_update))

        await app.initialize()
        me = await app.bot.get_me()
        self._bot_username = (me.username or "").lower()
        await self._register_commands(app)
        await app.start()
        assert app.updater is not None
        await app.updater.start_polling()
        logger.info("telegram: connected as @%s", self._bot_username)

    async def _register_commands(self, app: Application) -> None:
        if not self.commands:
            return
        try:
            await app.bot.set_my_commands(
                [BotCommand(cmd.name, (cmd.description or cmd.name)[:256]) for cmd in self.commands.all()]
            )
        except Exception:
            logger.exception("telegram: failed to register bot commands")

    async def _on_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if message is None or message.from_user is None or message.from_user.is_bot:
            return
        if not allow_list(self.allow_from, str(message.from_user.id)):
            logger.debug("telegram: message from unauthorized user %s", message.from_user.id)
            return

        content = message.text or message.caption or ""
        is_group = message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
        if is_group:
            needs_mention = not csv_contains(self.group_reply_all_chats, str(message.chat.id))
            if csv_contains(self.require_mention_chats, str(message.chat.id)):
                needs_mention = True

            mention = f"@{self._bot_username}" if self._bot_username else ""
            mentioned = bool(mention) and mention in content.lower()
            if needs_mention and not mentioned:
                return
            if mentioned:
                content = _strip_mention(content, mention)

        if not content and not message.photo and not message.document:
            return

        images, files = await _classify_attachments(message)
        chat_name = message.chat.title or message.chat.username or ""

        msg = PocketMessage(
            session_key=f"telegram:{message.chat.id}:{message.from_user.id}",
            channel_key=str(message.chat.id),
            platform="telegram",
            channel_id=str(message.chat.id),
            user_id=str(message.from_user.id),
            user_name=message.from_user.full_name or message.from_user.username or str(message.from_user.id),
            chat_name=chat_name,
            content=content,
            images=images,
            files=files,
            reply_ctx=message,
        )
        assert self._handler is not None
        await self._handler(self, msg)

    async def reply(self, reply_ctx, content: str) -> None:
        message: Message = reply_ctx
        for chunk in split_message(content, MAX_TELEGRAM_LEN):
            await message.reply_text(chunk)

    async def send(self, reply_ctx, content: str) -> None:
        message: Message = reply_ctx
        for chunk in split_message(content, MAX_TELEGRAM_LEN):
            await message.chat.send_message(chunk)

    def typing(self, reply_ctx):
        return _typing_indicator(reply_ctx)

    async def stop(self) -> None:
        if self._app is None:
            return
        if self._app.updater is not None:
            await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()


def _strip_mention(text: str, mention: str) -> str:
    lower = text.lower()
    idx = lower.find(mention)
    if idx == -1:
        return text.strip()
    return (text[:idx] + text[idx + len(mention) :]).strip()


@contextlib.asynccontextmanager
async def _typing_indicator(reply_ctx):
    message: Message = reply_ctx

    async def _loop() -> None:
        try:
            while True:
                await message.chat.send_action(ChatAction.TYPING)
                await asyncio.sleep(TYPING_REFRESH_SECONDS)
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
