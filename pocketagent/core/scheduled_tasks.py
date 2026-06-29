"""Fires a configured prompt into an existing channel session once a day,
posting the reply proactively instead of in response to an inbound message --
e.g. a nightly "summarize today's new vocabulary" digest.

Reuses Engine.on_message end-to-end (session locking, footer, the
usage-limit backlog) by constructing a synthetic Message whose reply_ctx
comes from Platform.make_channel_ctx rather than a real inbound message --
see core/platform.py and config.ScheduledTask.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .types import Message

if TYPE_CHECKING:
    from .engine import Engine
    from .platform import Platform

logger = logging.getLogger(__name__)


async def run_scheduled_task(
    engine: "Engine", platform: "Platform", channel_id: str, user_id: str, prompt: str
) -> None:
    session_key = f"{platform.name}:{channel_id}:{user_id}"
    if not engine.session_store.has_session(session_key):
        logger.info("scheduled task: no existing session for %s, skipping", session_key)
        return

    try:
        reply_ctx = await platform.make_channel_ctx(channel_id)
    except NotImplementedError:
        logger.warning(
            "scheduled task: platform %s does not support proactive channel sends",
            platform.name,
        )
        return

    msg = Message(
        session_key=session_key,
        platform=platform.name,
        channel_id=channel_id,
        channel_key=channel_id,
        user_id=user_id,
        user_name="scheduled-task",
        content=prompt,
        reply_ctx=reply_ctx,
    )
    await engine.on_message(platform, msg)
