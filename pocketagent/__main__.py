"""CLI entry point: `pocketagent run -c pocketagent.toml`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from .config import load_config, build_app


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pocketagent")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    run_parser = subparsers.add_parser("run", help="Run the bridge")
    run_parser.add_argument("-c", "--config", required=True, help="Path to pocketagent.toml")
    run_parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )

    return parser


async def _run(config_path: str) -> None:
    config = load_config(config_path)
    platforms, engine = build_app(config)

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

    try:
        await stop_event.wait()
    finally:
        logging.info("shutting down")
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
