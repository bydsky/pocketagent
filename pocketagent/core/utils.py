"""Agent-agnostic duration helpers shared by usage-limit-backlog code.

format_duration/parse_relative_duration are pure string<->timedelta helpers
with no knowledge of any particular agent's wording -- they're used both by
claude_code.py (to format/parse its own footer's "resets in" countdowns) and
by Engine (to format the "queued, retry in ~N" reply and to turn a proactive
100%-usage footer reading back into an absolute retry-at instant). Detecting
an agent's own usage-limit-denial error text is agent-specific and lives in
that agent's own module instead (see claude_code._parse_limit_denied).
"""

from __future__ import annotations

import re
from datetime import timedelta

_DURATION_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$")


def format_duration(delta: timedelta) -> str:
    """Compact countdown, e.g. "2h49m" under a day, else "2d"."""

    total_minutes = max(0, round(delta.total_seconds() / 60))
    days, rem = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem, 60)
    if days:
        return f"{days}d"
    if hours:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def parse_relative_duration(value: str) -> timedelta | None:
    """Inverse of format_duration: "2h49m" / "2d" / "11m" -> a timedelta."""

    if not value:
        return None
    match = _DURATION_RE.match(value.strip())
    if not match or not any(match.groups()):
        return None
    days, hours, minutes = (int(g) if g else 0 for g in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes)
