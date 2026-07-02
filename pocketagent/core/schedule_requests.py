"""Lets an agent add a new scheduled task itself, by including a fenced
```schedule-task``` TOML block anywhere in its chat reply, e.g.:

    ```schedule-task
    time = "09:00"
    timezone = "America/New_York"
    prompt = "Check on the build and report status."
    ```

or, for a recurring interval instead of a fixed daily time:

    ```schedule-task
    every = "2h"
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

from .scheduler import parse_time_of_day
from .utils import parse_relative_duration

_BLOCK_RE = re.compile(r"```schedule-task\s*\n(.*?)```", re.DOTALL)

SCHEDULE_TASK_INSTRUCTIONS = """\
If the user asks to be reminded of something, or wants you to check on or \
run something on a recurring basis going forward, you can schedule it \
yourself by including a fenced code block anywhere in your reply, exactly \
like one of these:

```schedule-task
time = "HH:MM"
timezone = ""
prompt = "..."
```

```schedule-task
every = "2h"
prompt = "..."
```

`prompt` (what to send yourself, reusing this conversation's history) is
always required, plus exactly one of: `time` (24h "HH:MM", for once a day --
`timezone` is an IANA name and may be omitted to use the local timezone) or
`every` (a recurring interval such as "30m", "2h", or "1d", for repeating
every that often starting from now). Only include this block when the user
actually wants something scheduled -- it will be removed from what the user
sees and replaced with a confirmation, so don't also describe its syntax to
them."""


@dataclass
class ScheduleRequest:
    prompt: str
    time: str = ""
    timezone: str = ""
    every: str = ""


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

    time_str = data.get("time", "")
    every = data.get("every", "")
    if not isinstance(time_str, str) or not isinstance(every, str):
        return ScheduleRequestError("schedule-task block's 'time'/'every' must be strings")
    if bool(time_str) == bool(every):
        return ScheduleRequestError("schedule-task block needs exactly one of 'time' (daily) or 'every' (interval)")

    if every:
        if parse_relative_duration(every) is None:
            return ScheduleRequestError(
                f'schedule-task block has an invalid "every" {every!r} (expected e.g. "2h", "30m", "1d")'
            )
        return ScheduleRequest(prompt=prompt, every=every)

    try:
        parse_time_of_day(time_str)
    except ValueError:
        return ScheduleRequestError(f'schedule-task block has an invalid time {time_str!r} (expected "HH:MM")')

    timezone = data.get("timezone", "")
    if not isinstance(timezone, str):
        return ScheduleRequestError("schedule-task block's 'timezone' must be a string")

    return ScheduleRequest(time=time_str, prompt=prompt, timezone=timezone)
