"""Lets an agent add a new scheduled task itself, by including a fenced
```schedule-task``` TOML block anywhere in its chat reply, e.g.:

    ```schedule-task
    time = "09:00"
    timezone = "America/New_York"
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

_BLOCK_RE = re.compile(r"```schedule-task\s*\n(.*?)```", re.DOTALL)

SCHEDULE_TASK_INSTRUCTIONS = """\
If the user asks to be reminded of something, or wants you to check on or \
run something once a day going forward, you can schedule it yourself by \
including a fenced code block anywhere in your reply, exactly like this:

```schedule-task
time = "HH:MM"
timezone = ""
prompt = "..."
```

`time` (24h "HH:MM") and `prompt` (what to send yourself at that time,
reusing this conversation's history) are required; `timezone` is an IANA
name and may be omitted to use the local timezone. Only include this block
when the user actually wants something scheduled -- it will be removed from
what the user sees and replaced with a confirmation, so don't also describe
its syntax to them."""


@dataclass
class ScheduleRequest:
    time: str
    prompt: str
    timezone: str = ""


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

    time_str = data.get("time")
    prompt = data.get("prompt")
    if not isinstance(time_str, str) or not time_str or not isinstance(prompt, str) or not prompt:
        return ScheduleRequestError("schedule-task block needs both 'time' and 'prompt' as strings")
    try:
        parse_time_of_day(time_str)
    except ValueError:
        return ScheduleRequestError(f'schedule-task block has an invalid time {time_str!r} (expected "HH:MM")')

    timezone = data.get("timezone", "")
    if not isinstance(timezone, str):
        return ScheduleRequestError("schedule-task block's 'timezone' must be a string")

    return ScheduleRequest(time=time_str, prompt=prompt, timezone=timezone)
