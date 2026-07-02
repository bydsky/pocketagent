"""Fires a configured prompt into an existing channel session on a schedule,
posting the reply proactively instead of in response to an inbound message --
e.g. a nightly "summarize today's new vocabulary" digest, or an
every-N-hours check-in.

Reuses Engine.on_message end-to-end (session locking, footer, the
usage-limit backlog) by constructing a synthetic Message whose reply_ctx
comes from Platform.make_channel_ctx rather than a real inbound message --
see core/platform.py and ScheduledTask below.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .types import Message
from .utils import parse_relative_duration

if TYPE_CHECKING:
    from .engine import Engine
    from .platform import Platform

logger = logging.getLogger(__name__)

SCHEDULED_TASKS_FILENAME = "scheduled_tasks.toml"


@dataclass
class ScheduledTask:
    """A prompt fired into one (platform, channel_id, user_id) session on a
    schedule, with the reply posted proactively to that channel. Exactly one
    of `time` (a fixed daily "HH:MM", paired with `timezone`) or `every` (a
    recurring interval like "2h"/"30m"/"1d", parsed by
    core.utils.parse_relative_duration) must be set -- see
    `_build_task_schedulers` in `__main__.py` for which of DailyScheduler /
    IntervalScheduler that becomes. A daily task naturally pairs with
    [daily_reset] (config.py): schedule it for just before the channel's
    reset time so the prompt still sees that day's conversation before it's
    cleared.
    """

    platform: str
    channel_id: str
    user_id: str
    prompt: str
    time: str = ""
    timezone: str = ""
    every: str = ""


def load_scheduled_tasks(config_dir: str | Path) -> list[ScheduledTask]:
    """Load `[[scheduled_tasks]]` from `scheduled_tasks.toml` next to the main config.

    Kept in its own file (unlike the rest of AppConfig) so it can be safely
    hot-reloaded on its own -- see `_reload_task_schedulers` in `__main__.py`
    -- without re-reading the main config file's platform tokens/secrets.
    """

    path = Path(config_dir) / SCHEDULED_TASKS_FILENAME
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text())
    tasks = []
    for raw in data.get("scheduled_tasks", []):
        time_str = raw.get("time", "")
        every = raw.get("every", "")
        channel_id = str(raw["channel_id"])
        if bool(time_str) == bool(every):
            raise ValueError(
                f"scheduled_tasks: entry for channel_id={channel_id!r} needs exactly "
                "one of 'time' (daily) or 'every' (interval)"
            )
        if every and parse_relative_duration(every) is None:
            raise ValueError(
                f"scheduled_tasks: entry for channel_id={channel_id!r} has an invalid "
                f"'every' {every!r} (expected e.g. \"2h\", \"30m\", \"1d\")"
            )
        tasks.append(
            ScheduledTask(
                platform=raw["platform"],
                channel_id=channel_id,
                user_id=str(raw["user_id"]),
                prompt=raw["prompt"],
                time=time_str,
                timezone=raw.get("timezone", ""),
                every=every,
            )
        )
    return tasks


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def format_scheduled_task_toml(task: ScheduledTask) -> str:
    lines = [
        "[[scheduled_tasks]]",
        f"platform = {_toml_string(task.platform)}",
        f"channel_id = {_toml_string(task.channel_id)}",
        f"user_id = {_toml_string(task.user_id)}",
    ]
    if task.every:
        lines.append(f"every = {_toml_string(task.every)}")
    else:
        lines.append(f"time = {_toml_string(task.time)}")
        if task.timezone:
            lines.append(f"timezone = {_toml_string(task.timezone)}")
    lines.append(f"prompt = {_toml_string(task.prompt)}")
    return "\n".join(lines) + "\n"


def append_scheduled_task(config_dir: str | Path, task: ScheduledTask) -> None:
    """Append `task` to scheduled_tasks.toml as a new `[[scheduled_tasks]]` block.

    Appends raw text instead of rewriting the whole file so any existing
    entries/comments/formatting are left untouched -- lets Engine (see
    core/schedule_requests.py) grant an agent the ability to add a scheduled
    task on a user's behalf without needing a real TOML writer. Runs
    synchronously with no `await` points, so it's safe to call without a
    lock from an asyncio event loop -- nothing else can interleave a
    concurrent write mid-call.
    """

    path = Path(config_dir) / SCHEDULED_TASKS_FILENAME
    needs_separator = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8") as f:
        if needs_separator:
            f.write("\n")
        f.write(format_scheduled_task_toml(task))


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
