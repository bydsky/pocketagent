from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from pocketagent.core.commands import CommandRegistry, CustomCommand
from pocketagent.core.platform import csv_contains
from pocketagent.platforms.discord_platform import DiscordPlatform


# --- csv_contains --------------------------------------------------------------


def test_csv_contains_empty_matches_nothing():
    assert csv_contains("", "123") is False


def test_csv_contains_star_matches_everything():
    assert csv_contains("*", "123") is True


def test_csv_contains_matches_member_case_insensitively():
    assert csv_contains("111, 222", "222") is True


def test_csv_contains_rejects_non_member():
    assert csv_contains("111,222", "333") is False


# --- DiscordPlatform mention gating --------------------------------------------


class _FakeUser:
    def __init__(self, id: int, bot: bool = False, display_name: str = ""):
        self.id = id
        self.bot = bot
        self.display_name = display_name or str(id)


class _FakeGuild:
    def __init__(self, id: int):
        self.id = id


class _FakeChannel:
    def __init__(self, id: int, name: str = ""):
        self.id = id
        self.name = name


class _FakeMessage:
    def __init__(self, content, author, channel, guild=None, mentions=None):
        self.content = content
        self.author = author
        self.channel = channel
        self.guild = guild
        self.mentions = mentions or []
        self.attachments = []


class _FakeClient:
    def __init__(self, user):
        self.user = user


def _wire(platform: DiscordPlatform, bot_id: int = 1):
    received = []

    async def handler(plat, msg):
        received.append(msg)

    platform._client = _FakeClient(_FakeUser(id=bot_id, bot=True))
    platform._handler = handler
    return received


@pytest.mark.asyncio
async def test_default_requires_mention_in_guild():
    platform = DiscordPlatform(token="t")
    received = _wire(platform)
    guild = _FakeGuild(id=555)
    channel = _FakeChannel(id=10)
    author = _FakeUser(id=2)

    await platform._on_discord_message(
        _FakeMessage("hello", author, channel, guild=guild, mentions=[])
    )
    assert received == []

    await platform._on_discord_message(
        _FakeMessage("<@1> hello", author, channel, guild=guild, mentions=[platform._client.user])
    )
    assert len(received) == 1
    assert received[0].content == "hello"


@pytest.mark.asyncio
async def test_dms_never_require_mention():
    platform = DiscordPlatform(token="t")
    received = _wire(platform)
    channel = _FakeChannel(id=10)
    author = _FakeUser(id=2)

    await platform._on_discord_message(
        _FakeMessage("hello", author, channel, guild=None, mentions=[])
    )
    assert len(received) == 1


@pytest.mark.asyncio
async def test_group_reply_all_guilds_skips_mention_requirement():
    platform = DiscordPlatform(token="t", group_reply_all_guilds="555")
    received = _wire(platform)
    guild = _FakeGuild(id=555)
    channel = _FakeChannel(id=10)
    author = _FakeUser(id=2)

    await platform._on_discord_message(
        _FakeMessage("hello", author, channel, guild=guild, mentions=[])
    )
    assert len(received) == 1


@pytest.mark.asyncio
async def test_group_reply_all_guilds_does_not_affect_other_guilds():
    platform = DiscordPlatform(token="t", group_reply_all_guilds="555")
    received = _wire(platform)
    other_guild = _FakeGuild(id=999)
    channel = _FakeChannel(id=10)
    author = _FakeUser(id=2)

    await platform._on_discord_message(
        _FakeMessage("hello", author, channel, guild=other_guild, mentions=[])
    )
    assert received == []


@pytest.mark.asyncio
async def test_require_mention_channels_overrides_group_reply_all_guild():
    platform = DiscordPlatform(
        token="t", group_reply_all_guilds="555", require_mention_channels="10"
    )
    received = _wire(platform)
    guild = _FakeGuild(id=555)
    strict_channel = _FakeChannel(id=10)
    normal_channel = _FakeChannel(id=11)
    author = _FakeUser(id=2)

    # Strict channel still requires @mention even though the guild is group_reply_all.
    await platform._on_discord_message(
        _FakeMessage("hello", author, strict_channel, guild=guild, mentions=[])
    )
    assert received == []

    # Normal channel in the same guild does not.
    await platform._on_discord_message(
        _FakeMessage("hello", author, normal_channel, guild=guild, mentions=[])
    )
    assert len(received) == 1


@pytest.mark.asyncio
async def test_group_reply_all_guilds_star_matches_all_guilds():
    platform = DiscordPlatform(token="t", group_reply_all_guilds="*")
    received = _wire(platform)
    channel = _FakeChannel(id=10)
    author = _FakeUser(id=2)

    await platform._on_discord_message(
        _FakeMessage("hello", author, channel, guild=_FakeGuild(id=1), mentions=[])
    )
    await platform._on_discord_message(
        _FakeMessage("hello", author, channel, guild=_FakeGuild(id=2), mentions=[])
    )
    assert len(received) == 2


# --- Slash commands -------------------------------------------------------------


def _fake_interaction(user_id: int, channel_id: int = 10, channel_name: str = "general"):
    interaction = Mock(spec=discord.Interaction)
    interaction.user = _FakeUser(id=user_id)
    interaction.channel_id = channel_id
    interaction.channel = _FakeChannel(id=channel_id, name=channel_name)
    interaction.response = Mock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = Mock()
    interaction.followup.send = AsyncMock()
    return interaction


def test_register_slash_commands_adds_one_per_custom_command():
    registry = CommandRegistry()
    registry.add(CustomCommand(name="english", prompt="Translate: {{1*}}", description="Translate text"))
    registry.add(CustomCommand(name="clear", prompt="/clear"))  # no description configured
    platform = DiscordPlatform(token="t", commands=registry)

    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.default()))
    platform._register_slash_commands(tree)

    registered = {c.name: c.description for c in tree.get_commands()}
    assert registered == {"english": "Translate text", "clear": "clear"}


def test_register_slash_commands_noop_without_commands():
    platform = DiscordPlatform(token="t")
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.default()))
    platform._register_slash_commands(tree)
    assert tree.get_commands() == []


@pytest.mark.asyncio
async def test_slash_command_dispatches_through_handler():
    registry = CommandRegistry()
    registry.add(CustomCommand(name="english", prompt="Translate: {{1*}}"))
    platform = DiscordPlatform(token="t", commands=registry)
    received = []

    async def handler(plat, msg):
        received.append(msg)

    platform._handler = handler
    interaction = _fake_interaction(user_id=2, channel_id=10)

    await platform._on_slash_command(interaction, "english", "hola mundo")

    interaction.response.defer.assert_awaited_once()
    assert len(received) == 1
    msg = received[0]
    assert msg.content == "/english hola mundo"
    assert msg.session_key == "discord:10:2"
    assert msg.reply_ctx is interaction


@pytest.mark.asyncio
async def test_slash_command_rejects_unauthorized_user():
    registry = CommandRegistry()
    registry.add(CustomCommand(name="english", prompt="Translate: {{1*}}"))
    platform = DiscordPlatform(token="t", allow_from="999", commands=registry)
    received = []

    async def handler(plat, msg):
        received.append(msg)

    platform._handler = handler
    interaction = _fake_interaction(user_id=2)

    await platform._on_slash_command(interaction, "english", "hola")

    interaction.response.send_message.assert_awaited_once()
    interaction.response.defer.assert_not_awaited()
    assert received == []


@pytest.mark.asyncio
async def test_reply_uses_followup_for_interaction():
    platform = DiscordPlatform(token="t")
    interaction = _fake_interaction(user_id=2)

    await platform.reply(interaction, "hi there")

    interaction.followup.send.assert_awaited_once_with("hi there")


@pytest.mark.asyncio
async def test_send_uses_followup_for_interaction():
    platform = DiscordPlatform(token="t")
    interaction = _fake_interaction(user_id=2)

    await platform.send(interaction, "hi there")

    interaction.followup.send.assert_awaited_once_with("hi there")
