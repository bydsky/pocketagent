"""Runs a callback once per day at a configured local time (e.g. daily session reset)."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time as dt_time, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# Arbitrary but fixed Monday, used only as a reference point so "every N
# weeks" (WeeklyScheduler's interval_weeks) always lands on the same
# calendar weeks regardless of when the task is loaded or the process
# restarts -- unlike IntervalScheduler, which has no natural absolute
# anchor and counts elapsed time from whenever start() was called instead.
_WEEK_EPOCH = date(2024, 1, 1)


def parse_time_of_day(value: str) -> dt_time:
    """Parse "HH:MM" (24h) into a time object."""

    hour_str, _, minute_str = value.partition(":")
    return dt_time(hour=int(hour_str), minute=int(minute_str or "0"))


def parse_weekday(value: str) -> int:
    """Parse a weekday name (case-insensitive) into Monday=0 .. Sunday=6."""

    key = value.strip().lower()
    if key not in _WEEKDAY_NAMES:
        raise ValueError(f'unknown weekday {value!r} (expected e.g. "monday")')
    return _WEEKDAY_NAMES[key]


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


def seconds_until_next(target: dt_time, tz: ZoneInfo | None, now: datetime | None = None) -> float:
    """Seconds from `now` until the next occurrence of `target` time-of-day, today or tomorrow."""

    now = now if now is not None else (datetime.now(tz) if tz is not None else datetime.now())
    return (next_occurrence(target, tz, now) - now).total_seconds()


def _weeks_since_epoch(d: date) -> int:
    return (d - _WEEK_EPOCH).days // 7


def next_weekly_occurrence(
    target: dt_time,
    weekday: int,
    interval_weeks: int,
    tz: ZoneInfo | None,
    now: datetime | None = None,
) -> datetime:
    """Absolute next occurrence of `target` time-of-day on `weekday`, restricted
    to weeks on the interval_weeks cadence (weeks-since-_WEEK_EPOCH divisible by
    interval_weeks) -- 1 means every week, 2 every other week, and so on.
    """

    now = now if now is not None else (datetime.now(tz) if tz is not None else datetime.now())
    candidate = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    days_ahead = (weekday - candidate.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    while _weeks_since_epoch(candidate.date()) % interval_weeks != 0:
        candidate += timedelta(days=7)
    return candidate


def seconds_until_next_weekly(
    target: dt_time,
    weekday: int,
    interval_weeks: int,
    tz: ZoneInfo | None,
    now: datetime | None = None,
) -> float:
    """Seconds from `now` until the next matching weekly occurrence."""

    now = now if now is not None else (datetime.now(tz) if tz is not None else datetime.now())
    return (next_weekly_occurrence(target, weekday, interval_weeks, tz, now) - now).total_seconds()


class DailyScheduler:
    """Sleeps until a configured time-of-day, then awaits callback() -- every day, forever."""

    def __init__(
        self,
        time_str: str,
        callback: Callable[[], Awaitable[None]],
        timezone: str = "",
    ) -> None:
        self._target = parse_time_of_day(time_str)
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
            delay = seconds_until_next(self._target, self._tz)
            logger.info("next daily run in %.0fs", delay)
            await asyncio.sleep(delay)
            try:
                await self._callback()
            except Exception:
                logger.exception("daily scheduler callback failed")


class WeeklyScheduler:
    """Sleeps until a configured time-of-day on a configured weekday, then
    awaits callback() -- every `interval_weeks` weeks (1 = every week, 2 =
    every other week, etc.), forever.

    Like DailyScheduler, each iteration recomputes the next matching instant
    from the current wall-clock time rather than adding a fixed offset, so
    it self-corrects across DST changes; interval_weeks parity is anchored
    to a fixed reference date (see _WEEK_EPOCH) rather than to whenever
    start() happened to be called, so which weeks are "on" doesn't drift or
    reset across restarts the way IntervalScheduler's elapsed-time cadence
    does.
    """

    def __init__(
        self,
        time_str: str,
        weekday: str,
        callback: Callable[[], Awaitable[None]],
        timezone: str = "",
        interval_weeks: int = 1,
    ) -> None:
        self._target = parse_time_of_day(time_str)
        self._weekday = parse_weekday(weekday)
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
            delay = seconds_until_next_weekly(self._target, self._weekday, self._interval_weeks, self._tz)
            logger.info("next weekly run in %.0fs", delay)
            await asyncio.sleep(delay)
            try:
                await self._callback()
            except Exception:
                logger.exception("weekly scheduler callback failed")


class IntervalScheduler:
    """Runs callback() repeatedly, every `interval`, forever.

    Unlike DailyScheduler (a fixed time-of-day), the first firing is
    `interval` after start() is called, not at some absolute clock time --
    e.g. an every="2h" task started at 10:15 next fires at 12:15, 14:15, ...
    """

    def __init__(self, interval: timedelta, callback: Callable[[], Awaitable[None]]) -> None:
        self._interval = interval
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
            await asyncio.sleep(self._interval.total_seconds())
            try:
                await self._callback()
            except Exception:
                logger.exception("interval scheduler callback failed")


class OneShotScheduler:
    """Sleeps until a specific absolute instant, then awaits callback() once.

    Unlike DailyScheduler, reschedule() can push the firing time later (e.g. a
    fresher signal reports a later reset) without losing whatever's already
    waiting on it; it's a no-op if the new time isn't later than the current
    one, so an earlier (more conservative) deadline is never shortened.
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
