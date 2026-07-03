"""CLI entry point: `pocketagent run -c pocketagent.toml`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from datetime import datetime
from pathlib import Path

from .config import build_app, build_reset_groups, load_config
from .core.scheduled_tasks import (
    SCHEDULED_TASKS_FILENAME,
    load_scheduled_tasks,
    remove_scheduled_task,
    run_scheduled_task,
)
from .core.scheduler import CronScheduler, OneShotScheduler, parse_run_at, resolve_timezone

SCHEDULED_TASKS_POLL_INTERVAL = 30.0


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pocketagent")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    run_parser = subparsers.add_parser("run", help="Run the bridge")
    run_parser.add_argument("-c", "--config", required=True, help="Path to pocketagent.toml")
    run_parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )

    return parser


def _build_reset_schedulers(config, engine) -> list[CronScheduler]:
    def _make_callback(predicate):
        return lambda: engine.clear_sessions(predicate)

    schedulers: list[CronScheduler] = []
    for group in build_reset_groups(config):
        try:
            schedulers.append(CronScheduler(group.cron, _make_callback(group.predicate()), group.timezone))
        except ValueError:
            logging.warning("daily_reset: invalid cron %r, skipping", group.cron)
    return schedulers


def _build_task_schedulers(
    scheduled_tasks, platforms, engine, config_dir: Path
) -> list[CronScheduler | OneShotScheduler]:
    def _make_task_callback(platform, task):
        return lambda: run_scheduled_task(engine, platform, task.channel_id, task.user_id, task.prompt)

    def _make_oneshot_callback(platform, task):
        async def _callback() -> None:
            await run_scheduled_task(engine, platform, task.channel_id, task.user_id, task.prompt)
            try:
                remove_scheduled_task(config_dir, task.id, task.platform, task.channel_id, task.user_id)
            except OSError:
                logging.exception("scheduled_tasks: failed to remove fired one-shot task %r", task.id)

        return _callback

    schedulers: list[CronScheduler | OneShotScheduler] = []
    for task in scheduled_tasks:
        platform = platforms.get(task.platform)
        if platform is None:
            logging.warning("scheduled_tasks: unknown platform '%s', skipping", task.platform)
            continue
        if task.run_at:
            try:
                run_at = parse_run_at(task.run_at, resolve_timezone(task.timezone))
            except ValueError:
                logging.warning("scheduled_tasks: invalid run_at %r, skipping", task.run_at)
                continue
            if run_at <= datetime.now(run_at.tzinfo):
                # Already due -- e.g. the process was down through its fire
                # time, or a previous firing's cleanup (see
                # _make_oneshot_callback) failed and left it behind. Clean it
                # up instead of building a OneShotScheduler for it: unlike a
                # future run_at, a past one would fire (almost) immediately,
                # silently re-sending a reminder the user already got (or
                # never got at the intended time either way).
                logging.info(
                    "scheduled_tasks: run_at %r for task %r already passed, cleaning up",
                    task.run_at,
                    task.id,
                )
                try:
                    remove_scheduled_task(config_dir, task.id, task.platform, task.channel_id, task.user_id)
                except OSError:
                    logging.exception("scheduled_tasks: failed to clean up expired task %r", task.id)
                continue
            schedulers.append(OneShotScheduler(run_at, _make_oneshot_callback(platform, task)))
            continue
        callback = _make_task_callback(platform, task)
        try:
            schedulers.append(CronScheduler(task.cron, callback, task.timezone, task.interval_weeks))
        except ValueError:
            logging.warning("scheduled_tasks: invalid cron %r, skipping", task.cron)
    return schedulers


async def _reload_reset_schedulers(config_path: str, engine, schedulers: list[CronScheduler]) -> None:
    """Re-read `config_path` and swap in daily-reset schedulers built from it.

    Triggered by SIGHUP, since picking up [daily_reset] and per-channel
    daily_reset_cron overrides means re-reading the main config file
    (platform tokens and all), unlike scheduled_tasks.toml which is watched
    automatically -- see `_watch_scheduled_tasks_file`.
    """

    try:
        config = load_config(config_path)
    except Exception:
        logging.exception("daily_reset reload: failed to load config from %s", config_path)
        return

    for scheduler in schedulers:
        await scheduler.stop()
    schedulers[:] = _build_reset_schedulers(config, engine)
    for scheduler in schedulers:
        scheduler.start()
    logging.info("daily_reset schedule reloaded (%d schedulers)", len(schedulers))


async def _reload_task_schedulers(
    config_dir: Path, platforms, engine, schedulers: list[CronScheduler | OneShotScheduler]
) -> None:
    try:
        scheduled_tasks = load_scheduled_tasks(config_dir)
    except Exception:
        logging.exception("scheduled_tasks reload: failed to load %s", config_dir / SCHEDULED_TASKS_FILENAME)
        return

    for scheduler in schedulers:
        await scheduler.stop()
    schedulers[:] = _build_task_schedulers(scheduled_tasks, platforms, engine, config_dir)
    for scheduler in schedulers:
        scheduler.start()
    logging.info("scheduled_tasks reloaded (%d schedulers)", len(schedulers))


async def _watch_scheduled_tasks_file(
    config_dir: Path,
    platforms,
    engine,
    schedulers: list[CronScheduler | OneShotScheduler],
    poll_interval: float = SCHEDULED_TASKS_POLL_INTERVAL,
) -> None:
    """Poll scheduled_tasks.toml's mtime and reload its schedulers on change.

    No secrets live in this file (unlike the main config), so it's safe to
    watch and reload automatically instead of requiring an operator to send
    SIGHUP.
    """

    path = config_dir / SCHEDULED_TASKS_FILENAME

    def _mtime() -> float | None:
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return None

    last_mtime = _mtime()
    while True:
        await asyncio.sleep(poll_interval)
        mtime = _mtime()
        if mtime != last_mtime:
            last_mtime = mtime
            await _reload_task_schedulers(config_dir, platforms, engine, schedulers)


async def _run(config_path: str) -> None:
    config = load_config(config_path)
    platforms, engine = build_app(config)
    config_dir = config.config_dir

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, AttributeError):
            pass  # signal handlers unsupported (e.g. Windows); rely on KeyboardInterrupt

    for name, platform in platforms.items():
        logging.info("starting platform %s", name)
        await platform.start(engine.on_message)

    reset_schedulers = _build_reset_schedulers(config, engine)
    task_schedulers = _build_task_schedulers(config.scheduled_tasks, platforms, engine, config_dir)

    try:
        loop.add_signal_handler(
            signal.SIGHUP,
            lambda: asyncio.create_task(_reload_reset_schedulers(config_path, engine, reset_schedulers)),
        )
    except (NotImplementedError, AttributeError):
        pass  # SIGHUP unsupported (e.g. Windows); daily_reset changes require a restart there

    for scheduler in reset_schedulers + task_schedulers:
        scheduler.start()

    watch_task = asyncio.create_task(
        _watch_scheduled_tasks_file(config_dir, platforms, engine, task_schedulers)
    )

    try:
        await stop_event.wait()
    finally:
        logging.info("shutting down")
        watch_task.cancel()
        try:
            await watch_task
        except asyncio.CancelledError:
            pass
        for scheduler in reset_schedulers + task_schedulers:
            await scheduler.stop()
        for platform in platforms.values():
            await platform.stop()
        await engine.shutdown()


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.cmd == "run":
        try:
            asyncio.run(_run(args.config))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
