"""Lets an agent add a new scheduled task itself, by including a fenced
```schedule-task``` TOML block anywhere in its chat reply, e.g.:

    ```schedule-task
    cron = "0 9 * * *"
    timezone = "America/New_York"
    prompt = "Check on the build and report status."
    ```

or, for every other week instead of every week the cron expression matches:

    ```schedule-task
    cron = "0 19 * * 4"
    interval_weeks = 2
    prompt = "Check on the build and report status."
    ```

Engine (core/engine.py) strips any such block out of the reply text before
it's sent to the platform and appends the parsed task to scheduled_tasks.toml
(core/scheduled_tasks.py) -- with platform/channel_id/user_id filled in from
the real incoming Message, never from the block itself, so the agent can't
schedule a task into a channel/user other than the one it's actually replying
to.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass

from .scheduler import validate_cron

_BLOCK_RE = re.compile(r"```schedule-task\s*\n(.*?)```", re.DOTALL)

SCHEDULE_TASK_INSTRUCTIONS = """\
If the user asks to be reminded of something, or wants you to check on or \
run something on a recurring basis going forward, you can schedule it \
yourself by including a fenced code block anywhere in your reply, exactly \
like this:

```schedule-task
cron = "0 9 * * *"
timezone = ""
interval_weeks = 1
prompt = "..."
```

`prompt` (what to send yourself, reusing this conversation's history) and
`cron` (a standard 5-field cron expression: minute hour day month weekday,
e.g. "0 9 * * *" for daily at 9am, "0 19 * * 4" for Thursdays at 19:00, "0
9 * * 1-5" for weekdays at 9am) are always required. `timezone` is an IANA
name and may be omitted to use the local timezone. `interval_weeks`
(default 1) is for "every Nth week" -- e.g. 2 to fire on only every other
week the cron expression matches, useful for a biweekly schedule.

Only include this block when the user actually wants something scheduled --
it will be removed from what the user sees and replaced with a confirmation,
so don't also describe its syntax to them."""


@dataclass
class ScheduleRequest:
    prompt: str
    cron: str
    timezone: str = ""
    interval_weeks: int = 1


@dataclass
class ScheduleRequestError:
    detail: str


def extract_schedule_requests(text: str) -> tuple[str, list[ScheduleRequest | ScheduleRequestError]]:
    """Strip ```schedule-task``` blocks out of `text`.

    Returns the cleaned text (blocks removed) and one ScheduleRequest -- or
    ScheduleRequestError, if a block was malformed -- per block found, in the
    order they appeared.
    """

    requests: list[ScheduleRequest | ScheduleRequestError] = []

    def _consume(match: re.Match[str]) -> str:
        requests.append(_parse_block(match.group(1)))
        return ""

    cleaned = _BLOCK_RE.sub(_consume, text).strip()
    return cleaned, requests


def _parse_block(body: str) -> ScheduleRequest | ScheduleRequestError:
    try:
        data = tomllib.loads(body)
    except tomllib.TOMLDecodeError as exc:
        return ScheduleRequestError(f"invalid schedule-task block ({exc})")

    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return ScheduleRequestError("schedule-task block needs a 'prompt' as a string")

    cron = data.get("cron")
    if not isinstance(cron, str) or not cron:
        return ScheduleRequestError("schedule-task block needs a 'cron' expression as a string")
    try:
        validate_cron(cron)
    except ValueError as exc:
        return ScheduleRequestError(f"schedule-task block: {exc}")

    timezone = data.get("timezone", "")
    if not isinstance(timezone, str):
        return ScheduleRequestError("schedule-task block's 'timezone' must be a string")

    interval_weeks = data.get("interval_weeks", 1)
    if not isinstance(interval_weeks, int) or isinstance(interval_weeks, bool) or interval_weeks < 1:
        return ScheduleRequestError("schedule-task block's 'interval_weeks' must be a positive integer")

    return ScheduleRequest(prompt=prompt, cron=cron, timezone=timezone, interval_weeks=interval_weeks)
