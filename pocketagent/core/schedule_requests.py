"""Lets an agent manage scheduled_tasks.toml (core/scheduled_tasks.py)
itself, by including one of three fenced blocks anywhere in its chat reply.

Add a task:

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

List the tasks scheduled for this conversation (a bare marker, no fields):

    ```list-scheduled-tasks
    ```

Remove one, by the id shown in an earlier add confirmation or list:

    ```remove-schedule-task
    id = "a1b2c3d4"
    ```

Engine (core/engine.py) strips any such block out of the reply text before
it's sent to the platform and acts on it against scheduled_tasks.toml --
with platform/channel_id/user_id always taken from the real incoming
Message, never from the block itself, so the agent can only ever
add/list/remove tasks tied to the channel/user it's actually replying to,
never another conversation's.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass

from .scheduler import validate_cron

_SCHEDULE_BLOCK_RE = re.compile(r"```schedule-task\s*\n(.*?)```", re.DOTALL)
_LIST_BLOCK_RE = re.compile(r"```list-scheduled-tasks\s*\n?.*?```", re.DOTALL)
_REMOVE_BLOCK_RE = re.compile(r"```remove-schedule-task\s*\n(.*?)```", re.DOTALL)

SCHEDULE_TASK_INSTRUCTIONS = """\
If the user asks to be reminded of something, or wants you to check on or \
run something on a recurring basis going forward, wants to know what's \
already scheduled, or wants to cancel something previously scheduled, you \
can manage that yourself with a fenced code block anywhere in your reply:

To add a schedule:
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

To see what's currently scheduled in this conversation:
```list-scheduled-tasks
```
This block takes no fields -- it will be replaced with the actual list
(each entry shown with its id).

To cancel one, using the id from an earlier add confirmation or list:
```remove-schedule-task
id = "a1b2c3d4"
```

Only include one of these blocks when the user actually wants to
schedule/list/cancel something -- each will be removed from what the user
sees and replaced with a confirmation or the requested list, so don't also
describe their syntax to them."""


@dataclass
class ScheduleRequest:
    prompt: str
    cron: str
    timezone: str = ""
    interval_weeks: int = 1


@dataclass
class RemoveTaskRequest:
    id: str


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
        requests.append(_parse_schedule_block(match.group(1)))
        return ""

    cleaned = _SCHEDULE_BLOCK_RE.sub(_consume, text).strip()
    return cleaned, requests


def extract_list_task_requests(text: str) -> tuple[str, int]:
    """Strip ```list-scheduled-tasks``` blocks out of `text`.

    They carry no fields, so this just reports how many were found (usually
    0 or 1) -- Engine fills in the actual listing itself.
    """

    count = 0

    def _consume(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return ""

    cleaned = _LIST_BLOCK_RE.sub(_consume, text).strip()
    return cleaned, count


def extract_remove_task_requests(text: str) -> tuple[str, list[RemoveTaskRequest | ScheduleRequestError]]:
    """Strip ```remove-schedule-task``` blocks out of `text`.

    Returns the cleaned text and one RemoveTaskRequest -- or
    ScheduleRequestError, if a block was malformed -- per block found.
    """

    requests: list[RemoveTaskRequest | ScheduleRequestError] = []

    def _consume(match: re.Match[str]) -> str:
        requests.append(_parse_remove_block(match.group(1)))
        return ""

    cleaned = _REMOVE_BLOCK_RE.sub(_consume, text).strip()
    return cleaned, requests


def _parse_schedule_block(body: str) -> ScheduleRequest | ScheduleRequestError:
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


def _parse_remove_block(body: str) -> RemoveTaskRequest | ScheduleRequestError:
    try:
        data = tomllib.loads(body)
    except tomllib.TOMLDecodeError as exc:
        return ScheduleRequestError(f"invalid remove-schedule-task block ({exc})")

    task_id = data.get("id")
    if not isinstance(task_id, str) or not task_id:
        return ScheduleRequestError("remove-schedule-task block needs an 'id' as a string")

    return RemoveTaskRequest(id=task_id)
