"""Runs a callback once per day at a configured local time (e.g. daily session reset)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as dt_time, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


def parse_time_of_day(value: str) -> dt_time:
    """Parse "HH:MM" (24h) into a time object."""

    hour_str, _, minute_str = value.partition(":")
    return dt_time(hour=int(hour_str), minute=int(minute_str or "0"))


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
