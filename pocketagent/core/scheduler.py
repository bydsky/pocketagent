"""Runs a callback on a schedule -- a cron expression (CronScheduler, e.g. for
daily session resets and scheduled_tasks.toml entries) or a specific absolute
instant once (OneShotScheduler, e.g. a usage-limit retry timer). next_occurrence
is also reused standalone by claude_code.py to parse a usage-limit denial's
own "resets HH:MM" wording into an absolute instant."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time as dt_time, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, croniter

logger = logging.getLogger(__name__)

# Arbitrary but fixed Monday, used only as a reference point so "every N
# weeks" (CronScheduler's interval_weeks) always lands on the same calendar
# weeks regardless of when the task is loaded or the process restarts.
# Standard cron has no native concept of "every Nth week" -- this is a
# bolt-on filter applied on top of whichever weeks the cron expression's own
# day-of-week field already matches.
_WEEK_EPOCH = date(2024, 1, 1)


def validate_cron(expr: str) -> None:
    """Raise ValueError if `expr` isn't a valid 5-field cron expression."""

    try:
        croniter(expr)
    except CroniterBadCronError as exc:
        raise ValueError(f"invalid cron expression {expr!r} ({exc})") from exc


def resolve_timezone(name: str) -> ZoneInfo | None:
    """Resolve an IANA timezone name, or None for the system local timezone."""

    if not name:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("unknown timezone %r, falling back to local time", name)
        return None


def next_occurrence(target: dt_time, tz: ZoneInfo | None, now: datetime | None = None) -> datetime:
    """Absolute next occurrence of `target` time-of-day, today or tomorrow, in tz (or local)."""

    now = now if now is not None else (datetime.now(tz) if tz is not None else datetime.now())
    next_run = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return next_run


def _weeks_since_epoch(d: date) -> int:
    return (d - _WEEK_EPOCH).days // 7


def next_cron_occurrence(
    expr: str,
    interval_weeks: int,
    tz: ZoneInfo | None,
    now: datetime | None = None,
) -> datetime:
    """Absolute next occurrence of cron expression `expr`, restricted to weeks
    on the interval_weeks cadence (weeks-since-_WEEK_EPOCH divisible by
    interval_weeks) -- 1 means every matching week, 2 every other, and so on.
    """

    now = now if now is not None else (datetime.now(tz) if tz is not None else datetime.now())
    itr = croniter(expr, now)
    candidate = itr.get_next(datetime)
    while _weeks_since_epoch(candidate.date()) % interval_weeks != 0:
        candidate = itr.get_next(datetime)
    return candidate


def seconds_until_next_cron(
    expr: str,
    interval_weeks: int,
    tz: ZoneInfo | None,
    now: datetime | None = None,
) -> float:
    """Seconds from `now` until the next matching cron occurrence."""

    now = now if now is not None else (datetime.now(tz) if tz is not None else datetime.now())
    return (next_cron_occurrence(expr, interval_weeks, tz, now) - now).total_seconds()


class CronScheduler:
    """Sleeps until the next occurrence of a 5-field cron expression, then
    awaits callback() -- forever, optionally restricted to every
    `interval_weeks` weeks (1 = every matching week, 2 = every other, etc.).

    Each iteration recomputes the next matching instant from the current
    wall-clock time rather than adding a fixed offset, so it self-corrects
    across DST changes; interval_weeks parity is anchored to a fixed
    reference date (see _WEEK_EPOCH) rather than to whenever start()
    happened to be called, so which weeks are "on" doesn't drift or reset
    across restarts.
    """

    def __init__(
        self,
        cron_expr: str,
        callback: Callable[[], Awaitable[None]],
        timezone: str = "",
        interval_weeks: int = 1,
    ) -> None:
        validate_cron(cron_expr)
        self._cron_expr = cron_expr
        self._interval_weeks = interval_weeks
        self._tz = resolve_timezone(timezone)
        self._callback = callback
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            delay = seconds_until_next_cron(self._cron_expr, self._interval_weeks, self._tz)
            logger.info("next cron run in %.0fs", delay)
            await asyncio.sleep(delay)
            try:
                await self._callback()
            except Exception:
                logger.exception("cron scheduler callback failed")


class OneShotScheduler:
    """Sleeps until a specific absolute instant, then awaits callback() once.

    Unlike the recurring schedulers above, reschedule() can push the firing
    time later (e.g. a fresher signal reports a later reset) without losing
    whatever's already waiting on it; it's a no-op if the new time isn't
    later than the current one, so an earlier (more conservative) deadline
    is never shortened.
    """

    def __init__(self, run_at: datetime, callback: Callable[[], Awaitable[None]]) -> None:
        self.run_at = run_at
        self._callback = callback
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def reschedule(self, run_at: datetime) -> None:
        if run_at <= self.run_at:
            return
        self.run_at = run_at
        if self._task is not None:
            self._task.cancel()
        self.start()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        delay = max(0.0, (self.run_at - datetime.now(self.run_at.tzinfo)).total_seconds())
        await asyncio.sleep(delay)
        try:
            await self._callback()
        except Exception:
            logger.exception("one-shot scheduler callback failed")
