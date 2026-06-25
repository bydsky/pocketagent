from unittest.mock import AsyncMock

import pytest
from slack_sdk.errors import SlackApiError

from pocketagent.core.platform import csv_contains
from pocketagent.platforms.slack_platform import SlackPlatform


# --- fakes -------------------------------------------------------------------


class _FakeWebClient:
    def __init__(self) -> None:
        self.chat_postMessage = AsyncMock()
        self.conversations_info = AsyncMock(return_value={"channel": {"name": "general"}})
        self.users_info = AsyncMock(
            return_value={"user": {"profile": {"display_name": "alice", "real_name": "Alice"}}}
        )


def _wire(platform: SlackPlatform, bot_user_id: str = "UBOT"):
    received = []

    async def handler(plat, msg):
        received.append(msg)

    platform._handler = handler
    platform._bot_user_id = bot_user_id
    platform._app = type("FakeApp", (), {"client": _FakeWebClient()})()
    return received


def _event(
    text: str,
    user: str = "U2",
    channel: str = "C10",
    channel_type: str = "im",
    files: list | None = None,
    bot_id: str | None = None,
    subtype: str | None = None,
) -> dict:
    event = {"text": text, "user": user, "channel": channel, "channel_type": channel_type}
    if files is not None:
        event["files"] = files
    if bot_id is not None:
        event["bot_id"] = bot_id
    if subtype is not None:
        event["subtype"] = subtype
    return event


# --- csv_contains (shared helper, sanity check reused import) -----------------


def test_csv_contains_star_matches_everything():
    assert csv_contains("*", "C10") is True


# --- construction --------------------------------------------------------------


def test_requires_bot_token():
    with pytest.raises(ValueError):
        SlackPlatform(bot_token="", app_token="xapp-1")


def test_requires_app_token():
    with pytest.raises(ValueError):
        SlackPlatform(bot_token="xoxb-1", app_token="")


# --- mention gating ------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_requires_mention_in_channel():
    platform = SlackPlatform(bot_token="t", app_token="a")
    received = _wire(platform)
    client = _FakeWebClient()

    await platform._on_message(_event("hello", channel_type="channel"), client)
    assert received == []

    await platform._on_message(_event("<@UBOT> hello", channel_type="channel"), client)
    assert len(received) == 1
    assert received[0].content == "hello"


@pytest.mark.asyncio
async def test_dms_never_require_mention():
    platform = SlackPlatform(bot_token="t", app_token="a")
    received = _wire(platform)
    client = _FakeWebClient()

    await platform._on_message(_event("hello", channel_type="im"), client)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_group_reply_all_channels_skips_mention_requirement():
    platform = SlackPlatform(bot_token="t", app_token="a", group_reply_all_channels="C10")
    received = _wire(platform)
    client = _FakeWebClient()

    await platform._on_message(_event("hello", channel="C10", channel_type="channel"), client)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_group_reply_all_channels_does_not_affect_other_channels():
    platform = SlackPlatform(bot_token="t", app_token="a", group_reply_all_channels="C10")
    received = _wire(platform)
    client = _FakeWebClient()

    await platform._on_message(_event("hello", channel="C99", channel_type="channel"), client)
    assert received == []


@pytest.mark.asyncio
async def test_require_mention_channels_overrides_group_reply_all_channels():
    platform = SlackPlatform(
        bot_token="t", app_token="a", group_reply_all_channels="*", require_mention_channels="C10"
    )
    received = _wire(platform)
    client = _FakeWebClient()

    await platform._on_message(_event("hello", channel="C10", channel_type="channel"), client)
    assert received == []

    await platform._on_message(_event("hello", channel="C11", channel_type="channel"), client)
    assert len(received) == 1


# --- allow_from / bot filtering ------------------------------------------------


@pytest.mark.asyncio
async def test_rejects_unauthorized_user():
    platform = SlackPlatform(bot_token="t", app_token="a", allow_from="U999")
    received = _wire(platform)
    client = _FakeWebClient()

    await platform._on_message(_event("hello"), client)
    assert received == []


@pytest.mark.asyncio
async def test_ignores_bot_messages():
    platform = SlackPlatform(bot_token="t", app_token="a")
    received = _wire(platform)
    client = _FakeWebClient()

    await platform._on_message(_event("hello", bot_id="B1"), client)
    assert received == []

    await platform._on_message(_event("hello", subtype="bot_message"), client)
    assert received == []


@pytest.mark.asyncio
async def test_ignores_own_messages():
    platform = SlackPlatform(bot_token="t", app_token="a")
    received = _wire(platform, bot_user_id="U2")
    client = _FakeWebClient()

    await platform._on_message(_event("hello", user="U2"), client)
    assert received == []


# --- session key / channel key --------------------------------------------------


@pytest.mark.asyncio
async def test_session_key_and_channel_key():
    platform = SlackPlatform(bot_token="t", app_token="a")
    received = _wire(platform)
    client = _FakeWebClient()

    await platform._on_message(_event("hello", user="U2", channel="C10"), client)
    msg = received[0]
    assert msg.session_key == "slack:C10:U2"
    assert msg.channel_key == "C10"
    assert msg.platform == "slack"
    assert msg.user_name == "alice"


@pytest.mark.asyncio
async def test_user_name_falls_back_to_id_on_api_error():
    platform = SlackPlatform(bot_token="t", app_token="a")
    received = _wire(platform)
    client = _FakeWebClient()
    client.users_info = AsyncMock(side_effect=SlackApiError("boom", {}))

    await platform._on_message(_event("hello", user="U2"), client)
    assert received[0].user_name == "U2"


# --- reply / send --------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_posts_message():
    platform = SlackPlatform(bot_token="t", app_token="a")
    web_client = _FakeWebClient()
    platform._app = type("FakeApp", (), {"client": web_client})()

    await platform.reply({"channel": "C10"}, "hello there")

    web_client.chat_postMessage.assert_awaited_once_with(channel="C10", text="hello there")


@pytest.mark.asyncio
async def test_send_posts_message():
    platform = SlackPlatform(bot_token="t", app_token="a")
    web_client = _FakeWebClient()
    platform._app = type("FakeApp", (), {"client": web_client})()

    await platform.send({"channel": "C10"}, "hello there")

    web_client.chat_postMessage.assert_awaited_once_with(channel="C10", text="hello there")


# --- channel name caching --------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_name_is_cached():
    platform = SlackPlatform(bot_token="t", app_token="a")
    received = _wire(platform)
    client = _FakeWebClient()

    await platform._on_message(_event("<@UBOT> hi", channel_type="channel"), client)
    await platform._on_message(_event("<@UBOT> hi again", channel_type="channel"), client)

    assert len(received) == 2
    assert received[0].chat_name == "general"
    client.conversations_info.assert_awaited_once()
