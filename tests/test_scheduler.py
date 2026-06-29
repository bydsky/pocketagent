import asyncio
from datetime import datetime, time

import pytest

from pocketagent.core.scheduler import (
    DailyScheduler,
    parse_time_of_day,
    resolve_timezone,
    seconds_until_next,
)


def test_parse_time_of_day():
    assert parse_time_of_day("04:30") == time(hour=4, minute=30)
    assert parse_time_of_day("4") == time(hour=4, minute=0)


def test_resolve_timezone_empty_returns_none():
    assert resolve_timezone("") is None


def test_resolve_timezone_unknown_falls_back_to_none():
    assert resolve_timezone("Not/AZone") is None


def test_seconds_until_next_later_today():
    now = datetime(2026, 6, 26, 1, 0, 0)
    target = time(hour=4, minute=0)
    assert seconds_until_next(target, None, now=now) == 3 * 3600


def test_seconds_until_next_rolls_over_to_tomorrow():
    now = datetime(2026, 6, 26, 5, 0, 0)
    target = time(hour=4, minute=0)
    assert seconds_until_next(target, None, now=now) == 23 * 3600


def test_seconds_until_next_at_exact_target_rolls_over():
    now = datetime(2026, 6, 26, 4, 0, 0)
    target = time(hour=4, minute=0)
    assert seconds_until_next(target, None, now=now) == 24 * 3600


@pytest.mark.asyncio
async def test_daily_scheduler_runs_callback_and_can_be_stopped(monkeypatch):
    calls = []

    async def callback():
        calls.append(1)

    # Force the sleep to resolve immediately so the callback fires fast in the test.
    monkeypatch.setattr(
        "pocketagent.core.scheduler.seconds_until_next", lambda *a, **k: 0
    )

    scheduler = DailyScheduler("04:00", callback)
    scheduler.start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert calls

    await scheduler.stop()


@pytest.mark.asyncio
async def test_daily_scheduler_keeps_running_after_callback_raises(monkeypatch):
    calls = []

    async def flaky_callback():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "pocketagent.core.scheduler.seconds_until_next", lambda *a, **k: 0
    )

    scheduler = DailyScheduler("04:00", flaky_callback)
    scheduler.start()
    for _ in range(5):
        await asyncio.sleep(0)
        if len(calls) >= 2:
            break

    assert len(calls) >= 2

    await scheduler.stop()
