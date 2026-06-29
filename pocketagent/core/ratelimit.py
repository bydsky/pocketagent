"""Detects a usage-limit denial and turns it into a retry-at instant.

Two independent signals feed the same retry-at instant, used by Engine to
queue messages instead of erroring/sending while an agent backend's usage
limit is exhausted:

  - reactive: the agent backend's own error text for a denied request, e.g.
    claude_code's "You've hit your session limit · resets 2:50pm
    (Australia/Sydney)" (see parse_denial_reset_at). Always tz-aware, so it's
    safe to compare against the proactive signal below.
  - proactive: a usage percentage already at/over 100% from a successful
    turn's footer data (rate_limit_5h_pct/7d_pct), paired with its "resets
    in" countdown string (e.g. "2h49m") -- parse_relative_duration is the
    inverse of format_duration below, which claude_code.py also uses to
    produce that same countdown string for the footer.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .scheduler import next_occurrence, resolve_timezone

_DENIAL_RE = re.compile(
    r"hit your \w+ limit\D*?resets\s+(\d{1,2}:\d{2}\s*[ap]m)\s*\(([^)]+)\)",
    re.IGNORECASE,
)


def parse_denial_reset_at(error_text: str, now: datetime | None = None) -> datetime | None:
    """Parse a usage-limit-denial error into the absolute instant it next
    resets (today or tomorrow, in the denial's own timezone) -- or None if
    error_text isn't this kind of denial.

    Falls back to UTC if the timezone name is missing/unrecognized, so the
    result is always tz-aware (never the "naive local time" fallback that
    resolve_timezone/next_occurrence use elsewhere for daily_reset, which
    would make it unsafe to compare against the proactive signal's UTC
    instant).
    """

    match = _DENIAL_RE.search(error_text)
    if not match:
        return None
    time_str, tz_name = match.group(1), match.group(2)
    tz = resolve_timezone(tz_name) or timezone.utc
    try:
        target = datetime.strptime(time_str.replace(" ", "").upper(), "%I:%M%p").time()
    except ValueError:
        return None
    # now, if given, may be in any tz (e.g. UTC) -- convert into tz first so
    # replacing hour/minute lands on the right wall-clock instant.
    now_in_tz = now.astimezone(tz) if now is not None else None
    return next_occurrence(target, tz, now_in_tz)


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
