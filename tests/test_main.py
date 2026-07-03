from datetime import datetime, timedelta

from pocketagent.__main__ import _build_task_schedulers
from pocketagent.core.scheduled_tasks import ScheduledTask, append_scheduled_task, load_scheduled_tasks
from pocketagent.core.scheduler import CronScheduler, OneShotScheduler


class _FakePlatform:
    name = "discord"


class _FakeEngine:
    pass


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def test_build_task_schedulers_cron_task_builds_cron_scheduler(tmp_path):
    task = ScheduledTask(platform="discord", channel_id="1", user_id="1", cron="0 9 * * *", prompt="hi")
    append_scheduled_task(tmp_path, task)
    tasks = load_scheduled_tasks(tmp_path)

    schedulers = _build_task_schedulers(tasks, {"discord": _FakePlatform()}, _FakeEngine(), tmp_path)

    assert len(schedulers) == 1
    assert isinstance(schedulers[0], CronScheduler)


def test_build_task_schedulers_future_run_at_builds_one_shot_scheduler(tmp_path):
    future = _iso(datetime.now() + timedelta(days=365))
    task = ScheduledTask(platform="discord", channel_id="1", user_id="1", run_at=future, prompt="hi")
    append_scheduled_task(tmp_path, task)
    tasks = load_scheduled_tasks(tmp_path)

    schedulers = _build_task_schedulers(tasks, {"discord": _FakePlatform()}, _FakeEngine(), tmp_path)

    assert len(schedulers) == 1
    assert isinstance(schedulers[0], OneShotScheduler)
    assert load_scheduled_tasks(tmp_path) == tasks  # untouched -- still pending


def test_build_task_schedulers_expired_run_at_is_cleaned_up_and_skipped(tmp_path):
    past = _iso(datetime.now() - timedelta(days=1))
    task = ScheduledTask(platform="discord", channel_id="1", user_id="1", run_at=past, prompt="hi")
    task_id = append_scheduled_task(tmp_path, task)

    tasks = load_scheduled_tasks(tmp_path)
    schedulers = _build_task_schedulers(tasks, {"discord": _FakePlatform()}, _FakeEngine(), tmp_path)

    assert schedulers == []
    remaining = load_scheduled_tasks(tmp_path)
    assert all(t.id != task_id for t in remaining)


def test_build_task_schedulers_expired_run_at_does_not_affect_other_entries(tmp_path):
    past = _iso(datetime.now() - timedelta(days=1))
    expired = ScheduledTask(platform="discord", channel_id="1", user_id="1", run_at=past, prompt="expired")
    keeper = ScheduledTask(platform="discord", channel_id="2", user_id="2", cron="0 9 * * *", prompt="keeper")
    append_scheduled_task(tmp_path, expired)
    keeper_id = append_scheduled_task(tmp_path, keeper)

    tasks = load_scheduled_tasks(tmp_path)
    schedulers = _build_task_schedulers(tasks, {"discord": _FakePlatform()}, _FakeEngine(), tmp_path)

    assert len(schedulers) == 1
    assert isinstance(schedulers[0], CronScheduler)
    remaining = load_scheduled_tasks(tmp_path)
    assert len(remaining) == 1
    assert remaining[0].id == keeper_id


def test_build_task_schedulers_invalid_run_at_skips_without_cleanup(tmp_path, monkeypatch):
    # Bypass load_scheduled_tasks's own validation to exercise
    # _build_task_schedulers's defensive handling of a malformed run_at
    # directly (e.g. hand-edited into the file).
    task = ScheduledTask(platform="discord", channel_id="1", user_id="1", run_at="not-a-datetime", prompt="hi")

    schedulers = _build_task_schedulers([task], {"discord": _FakePlatform()}, _FakeEngine(), tmp_path)

    assert schedulers == []
    assert not (tmp_path / "scheduled_tasks.toml").exists()


def test_build_task_schedulers_unknown_platform_skips(tmp_path):
    task = ScheduledTask(platform="discord", channel_id="1", user_id="1", cron="0 9 * * *", prompt="hi")

    schedulers = _build_task_schedulers([task], {}, _FakeEngine(), tmp_path)

    assert schedulers == []
