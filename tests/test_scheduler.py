import asyncio
from datetime import datetime, time, timedelta

import pytest

from pocketagent.core.scheduler import (
    CronScheduler,
    OneShotScheduler,
    next_cron_occurrence,
    next_occurrence,
    resolve_timezone,
    seconds_until_next_cron,
    validate_cron,
)


def test_resolve_timezone_empty_returns_none():
    assert resolve_timezone("") is None


def test_resolve_timezone_unknown_falls_back_to_none():
    assert resolve_timezone("Not/AZone") is None


def test_next_occurrence_later_today():
    now = datetime(2026, 6, 26, 1, 0, 0)
    target = time(hour=4, minute=0)
    assert next_occurrence(target, None, now=now) == datetime(2026, 6, 26, 4, 0, 0)


def test_next_occurrence_rolls_over_to_tomorrow():
    now = datetime(2026, 6, 26, 5, 0, 0)
    target = time(hour=4, minute=0)
    assert next_occurrence(target, None, now=now) == datetime(2026, 6, 27, 4, 0, 0)


@pytest.mark.asyncio
async def test_one_shot_scheduler_fires_once_and_can_be_stopped():
    calls = []

    async def callback():
        calls.append(1)

    scheduler = OneShotScheduler(datetime.now() - timedelta(seconds=1), callback)
    scheduler.start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert calls == [1]

    await scheduler.stop()


@pytest.mark.asyncio
async def test_one_shot_scheduler_reschedule_pushes_later_only():
    far_future = datetime.now() + timedelta(days=365)

    async def noop():
        pass

    scheduler = OneShotScheduler(far_future, noop)

    scheduler.reschedule(far_future - timedelta(days=1))  # earlier -- ignored
    assert scheduler.run_at == far_future

    scheduler.reschedule(far_future + timedelta(days=1))  # later -- applied
    assert scheduler.run_at == far_future + timedelta(days=1)

    await scheduler.stop()


def test_validate_cron_accepts_valid_expression():
    validate_cron("0 19 * * 4")  # doesn't raise


def test_validate_cron_rejects_invalid_expression():
    with pytest.raises(ValueError):
        validate_cron("not a cron expression")


def test_next_cron_occurrence_same_day_before_target_time():
    # 2026-07-02 is a Thursday.
    now = datetime(2026, 7, 2, 10, 0)
    assert next_cron_occurrence("0 19 * * 4", 1, None, now=now) == datetime(2026, 7, 2, 19, 0)


def test_next_cron_occurrence_same_day_after_target_time_rolls_to_next_week():
    now = datetime(2026, 7, 2, 20, 0)
    assert next_cron_occurrence("0 19 * * 4", 1, None, now=now) == datetime(2026, 7, 9, 19, 0)


def test_next_cron_occurrence_different_day_rolls_forward():
    # 2026-06-29 is a Monday; next Thursday is 2026-07-02.
    now = datetime(2026, 6, 29, 8, 0)
    assert next_cron_occurrence("0 19 * * 4", 1, None, now=now) == datetime(2026, 7, 2, 19, 0)


def test_next_cron_occurrence_biweekly_gap_is_exactly_two_weeks():
    now = datetime(2026, 7, 2, 10, 0)
    first = next_cron_occurrence("0 19 * * 4", 2, None, now=now)
    second = next_cron_occurrence("0 19 * * 4", 2, None, now=first + timedelta(seconds=1))
    assert (second - first) == timedelta(weeks=2)
    assert first.strftime("%A") == second.strftime("%A") == "Thursday"


def test_next_cron_occurrence_daily_expression():
    now = datetime(2026, 6, 26, 1, 0, 0)
    assert next_cron_occurrence("0 4 * * *", 1, None, now=now) == datetime(2026, 6, 26, 4, 0, 0)


def test_seconds_until_next_cron():
    now = datetime(2026, 7, 2, 10, 0)
    assert seconds_until_next_cron("0 19 * * 4", 1, None, now=now) == 9 * 3600


@pytest.mark.asyncio
async def test_cron_scheduler_runs_callback_and_can_be_stopped(monkeypatch):
    calls = []

    async def callback():
        calls.append(1)

    monkeypatch.setattr(
        "pocketagent.core.scheduler.seconds_until_next_cron", lambda *a, **k: 0
    )

    scheduler = CronScheduler("0 19 * * 4", callback)
    scheduler.start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert calls

    await scheduler.stop()


@pytest.mark.asyncio
async def test_cron_scheduler_keeps_running_after_callback_raises(monkeypatch):
    calls = []

    async def flaky_callback():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "pocketagent.core.scheduler.seconds_until_next_cron", lambda *a, **k: 0
    )

    scheduler = CronScheduler("0 19 * * 4", flaky_callback)
    scheduler.start()
    for _ in range(5):
        await asyncio.sleep(0)
        if len(calls) >= 2:
            break

    assert len(calls) >= 2

    await scheduler.stop()


def test_cron_scheduler_construction_rejects_invalid_cron():
    async def callback():
        pass

    with pytest.raises(ValueError):
        CronScheduler("not a cron expression", callback)
