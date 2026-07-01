import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.constants import ChatType

from pocketagent.core.commands import CommandRegistry, CustomCommand
from pocketagent.core.platform import csv_contains
from pocketagent.platforms.telegram_platform import TelegramPlatform


# --- fakes -------------------------------------------------------------------


class _FakeUser:
    def __init__(self, id: int, is_bot: bool = False, full_name: str = "", username: str = ""):
        self.id = id
        self.is_bot = is_bot
        self.full_name = full_name or str(id)
        self.username = username


class _FakeChat:
    def __init__(self, id: int, type: str = ChatType.PRIVATE, title: str = "", username: str = ""):
        self.id = id
        self.type = type
        self.title = title
        self.username = username
        self.send_message = AsyncMock()
        self.send_action = AsyncMock()


class _FakeMessage:
    def __init__(self, text, from_user, chat, caption=""):
        self.text = text
        self.caption = caption
        self.from_user = from_user
        self.chat = chat
        self.photo = []
        self.document = None
        self.reply_text = AsyncMock()
        self.reply_to_message = None


class _FakeUpdate:
    def __init__(self, message):
        self.message = message


def _wire(platform: TelegramPlatform, bot_username: str = "mybot"):
    received = []

    async def handler(plat, msg):
        received.append(msg)

    platform._handler = handler
    platform._bot_username = bot_username
    return received


# --- csv_contains (shared helper, sanity check reused import) -----------------


def test_csv_contains_star_matches_everything():
    assert csv_contains("*", "123") is True


# --- mention gating ------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_requires_mention_in_group():
    platform = TelegramPlatform(token="t")
    received = _wire(platform)
    chat = _FakeChat(id=10, type=ChatType.GROUP)
    author = _FakeUser(id=2)

    await platform._on_update(_FakeUpdate(_FakeMessage("hello", author, chat)), None)
    assert received == []

    await platform._on_update(_FakeUpdate(_FakeMessage("@mybot hello", author, chat)), None)
    assert len(received) == 1
    assert received[0].content == "hello"


@pytest.mark.asyncio
async def test_private_chats_never_require_mention():
    platform = TelegramPlatform(token="t")
    received = _wire(platform)
    chat = _FakeChat(id=10, type=ChatType.PRIVATE)
    author = _FakeUser(id=2)

    await platform._on_update(_FakeUpdate(_FakeMessage("hello", author, chat)), None)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_group_reply_all_chats_skips_mention_requirement():
    platform = TelegramPlatform(token="t", group_reply_all_chats="10")
    received = _wire(platform)
    chat = _FakeChat(id=10, type=ChatType.GROUP)
    author = _FakeUser(id=2)

    await platform._on_update(_FakeUpdate(_FakeMessage("hello", author, chat)), None)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_group_reply_all_chats_does_not_affect_other_chats():
    platform = TelegramPlatform(token="t", group_reply_all_chats="10")
    received = _wire(platform)
    other_chat = _FakeChat(id=99, type=ChatType.GROUP)
    author = _FakeUser(id=2)

    await platform._on_update(_FakeUpdate(_FakeMessage("hello", author, other_chat)), None)
    assert received == []


@pytest.mark.asyncio
async def test_require_mention_chats_overrides_group_reply_all_chat():
    platform = TelegramPlatform(token="t", group_reply_all_chats="*", require_mention_chats="10")
    received = _wire(platform)
    strict_chat = _FakeChat(id=10, type=ChatType.GROUP)
    normal_chat = _FakeChat(id=11, type=ChatType.GROUP)
    author = _FakeUser(id=2)

    await platform._on_update(_FakeUpdate(_FakeMessage("hello", author, strict_chat)), None)
    assert received == []

    await platform._on_update(_FakeUpdate(_FakeMessage("hello", author, normal_chat)), None)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_supergroup_treated_like_group():
    platform = TelegramPlatform(token="t")
    received = _wire(platform)
    chat = _FakeChat(id=10, type=ChatType.SUPERGROUP)
    author = _FakeUser(id=2)

    await platform._on_update(_FakeUpdate(_FakeMessage("hello", author, chat)), None)
    assert received == []


# --- allow_from / bot filtering ------------------------------------------------


@pytest.mark.asyncio
async def test_rejects_unauthorized_user():
    platform = TelegramPlatform(token="t", allow_from="999")
    received = _wire(platform)
    chat = _FakeChat(id=10, type=ChatType.PRIVATE)
    author = _FakeUser(id=2)

    await platform._on_update(_FakeUpdate(_FakeMessage("hello", author, chat)), None)
    assert received == []


@pytest.mark.asyncio
async def test_ignores_messages_from_bots():
    platform = TelegramPlatform(token="t")
    received = _wire(platform)
    chat = _FakeChat(id=10, type=ChatType.PRIVATE)
    author = _FakeUser(id=2, is_bot=True)

    await platform._on_update(_FakeUpdate(_FakeMessage("hello", author, chat)), None)
    assert received == []


@pytest.mark.asyncio
async def test_session_key_and_channel_key():
    platform = TelegramPlatform(token="t")
    received = _wire(platform)
    chat = _FakeChat(id=10, type=ChatType.PRIVATE)
    author = _FakeUser(id=2)

    await platform._on_update(_FakeUpdate(_FakeMessage("hello", author, chat)), None)
    msg = received[0]
    assert msg.session_key == "telegram:10:2"
    assert msg.channel_key == "10"
    assert msg.platform == "telegram"


# --- reply / send --------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_sends_via_reply_text():
    platform = TelegramPlatform(token="t")
    chat = _FakeChat(id=10)
    message = _FakeMessage("hi", _FakeUser(id=2), chat)

    await platform.reply(message, "hello there")

    message.reply_text.assert_awaited_once_with("hello there")


@pytest.mark.asyncio
async def test_send_sends_via_chat_send_message():
    platform = TelegramPlatform(token="t")
    chat = _FakeChat(id=10)
    message = _FakeMessage("hi", _FakeUser(id=2), chat)

    await platform.send(message, "hello there")

    chat.send_message.assert_awaited_once_with("hello there")


# --- typing indicator ------------------------------------------------------------


@pytest.mark.asyncio
async def test_typing_sends_chat_action():
    platform = TelegramPlatform(token="t")
    chat = _FakeChat(id=10)
    message = _FakeMessage("hi", _FakeUser(id=2), chat)

    async with platform.typing(message):
        await asyncio.sleep(0)
        chat.send_action.assert_awaited()


# --- registering bot commands ---------------------------------------------------


@pytest.mark.asyncio
async def test_register_commands_calls_set_my_commands():
    registry = CommandRegistry()
    registry.add(CustomCommand(name="deploy", prompt="Deploy {{1}}", description="Deploy a service"))
    platform = TelegramPlatform(token="t", commands=registry)

    app = Mock()
    app.bot.set_my_commands = AsyncMock()

    await platform._register_commands(app)

    app.bot.set_my_commands.assert_awaited_once()
    sent = app.bot.set_my_commands.call_args.args[0]
    assert sent[0].command == "deploy"
    assert sent[0].description == "Deploy a service"


@pytest.mark.asyncio
async def test_register_commands_noop_without_commands():
    platform = TelegramPlatform(token="t")
    app = Mock()
    app.bot.set_my_commands = AsyncMock()

    await platform._register_commands(app)

    app.bot.set_my_commands.assert_not_awaited()
