"""Fires a configured prompt into an existing channel session on a schedule,
posting the reply proactively instead of in response to an inbound message --
e.g. a nightly "summarize today's new vocabulary" digest, or a weekly
check-in.

Reuses Engine.on_message end-to-end (session locking, footer, the
usage-limit backlog) by constructing a synthetic Message whose reply_ctx
comes from Platform.make_channel_ctx rather than a real inbound message --
see core/platform.py and ScheduledTask below.
"""

from __future__ import annotations

import logging
import tomllib
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .scheduler import validate_cron
from .types import Message

if TYPE_CHECKING:
    from .engine import Engine
    from .platform import Platform

logger = logging.getLogger(__name__)

SCHEDULED_TASKS_FILENAME = "scheduled_tasks.toml"


def generate_task_id() -> str:
    """A short opaque id for a ScheduledTask, unique enough for this use case."""

    return uuid.uuid4().hex[:8]


@dataclass
class ScheduledTask:
    """A prompt fired into one (platform, channel_id, user_id) session on a
    schedule, with the reply posted proactively to that channel.

    `id` identifies this entry (e.g. for a future list/remove feature).
    Entries written by `append_scheduled_task` always get a persisted,
    stable one; entries loaded from a hand-edited scheduled_tasks.toml that
    doesn't set `id` get one generated fresh on each load instead (not
    written back, to avoid rewriting -- and losing comments/formatting in --
    the rest of the file) so it isn't guaranteed stable across reloads
    unless you set `id` explicitly.

    `cron` is a standard 5-field cron expression (minute hour day month
    weekday, e.g. "0 19 * * 4" for Thursdays at 19:00), evaluated in
    `timezone` (an IANA name; omit for local time) -- see
    core.scheduler.CronScheduler. `interval_weeks` (default 1) is a bolt-on
    on top of that: standard cron has no native "every Nth week", so 2
    means only every other week the cron expression matches actually fires
    (anchored to a fixed reference date, not to whenever the task loads).

    A daily cron naturally pairs with [daily_reset] (config.py): schedule
    it for just before the channel's reset time so the prompt still sees
    that day's conversation before it's cleared.
    """

    platform: str
    channel_id: str
    user_id: str
    prompt: str
    cron: str
    id: str = ""
    timezone: str = ""
    interval_weeks: int = 1


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
        channel_id = str(raw["channel_id"])
        cron_expr = raw.get("cron", "")
        if not cron_expr:
            raise ValueError(f"scheduled_tasks: entry for channel_id={channel_id!r} needs a 'cron' expression")
        try:
            validate_cron(cron_expr)
        except ValueError as exc:
            raise ValueError(f"scheduled_tasks: entry for channel_id={channel_id!r}: {exc}") from exc

        interval_weeks = raw.get("interval_weeks", 1)
        if not isinstance(interval_weeks, int) or isinstance(interval_weeks, bool) or interval_weeks < 1:
            raise ValueError(
                f"scheduled_tasks: entry for channel_id={channel_id!r} has an invalid "
                f"'interval_weeks' {interval_weeks!r} (expected a positive integer)"
            )

        task_id = raw.get("id", "")
        if not isinstance(task_id, str):
            raise ValueError(f"scheduled_tasks: entry for channel_id={channel_id!r} has a non-string 'id'")

        tasks.append(
            ScheduledTask(
                platform=raw["platform"],
                channel_id=channel_id,
                user_id=str(raw["user_id"]),
                prompt=raw["prompt"],
                cron=cron_expr,
                id=task_id or generate_task_id(),
                timezone=raw.get("timezone", ""),
                interval_weeks=interval_weeks,
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
    lines = ["[[scheduled_tasks]]"]
    if task.id:
        lines.append(f"id = {_toml_string(task.id)}")
    lines += [
        f"platform = {_toml_string(task.platform)}",
        f"channel_id = {_toml_string(task.channel_id)}",
        f"user_id = {_toml_string(task.user_id)}",
        f"cron = {_toml_string(task.cron)}",
    ]
    if task.timezone:
        lines.append(f"timezone = {_toml_string(task.timezone)}")
    if task.interval_weeks != 1:
        lines.append(f"interval_weeks = {task.interval_weeks}")
    lines.append(f"prompt = {_toml_string(task.prompt)}")
    return "\n".join(lines) + "\n"


def append_scheduled_task(config_dir: str | Path, task: ScheduledTask) -> str:
    """Append `task` to scheduled_tasks.toml as a new `[[scheduled_tasks]]` block.

    Assigns `task.id` a fresh generate_task_id() if it doesn't already have
    one, so every appended entry gets a real, persisted id -- unlike
    entries loaded straight from a hand-edited file that skip `id`, which
    only get one generated fresh in memory on each load (see
    load_scheduled_tasks). Returns the id that was written.

    Appends raw text instead of rewriting the whole file so any existing
    entries/comments/formatting are left untouched -- lets Engine (see
    core/schedule_requests.py) grant an agent the ability to add a scheduled
    task on a user's behalf without needing a real TOML writer. Runs
    synchronously with no `await` points, so it's safe to call without a
    lock from an asyncio event loop -- nothing else can interleave a
    concurrent write mid-call.
    """

    if not task.id:
        task = replace(task, id=generate_task_id())

    path = Path(config_dir) / SCHEDULED_TASKS_FILENAME
    needs_separator = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8") as f:
        if needs_separator:
            f.write("\n")
        f.write(format_scheduled_task_toml(task))
    return task.id


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
