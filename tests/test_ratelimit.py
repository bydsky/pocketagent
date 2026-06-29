from datetime import datetime, timedelta, timezone

from pocketagent.core.ratelimit import (
    format_duration,
    parse_denial_reset_at,
    parse_relative_duration,
)


def test_parse_denial_reset_at_extracts_time_and_timezone():
    now = datetime(2026, 6, 26, 14, 0, 0, tzinfo=timezone.utc)
    retry_at = parse_denial_reset_at(
        "You've hit your session limit · resets 2:50pm (Australia/Sydney)", now=now
    )

    assert retry_at is not None
    assert retry_at.tzinfo is not None
    assert (retry_at.hour, retry_at.minute) == (14, 50)


def test_parse_denial_reset_at_rolls_over_to_tomorrow_when_time_already_passed():
    now = datetime(2026, 6, 26, 23, 0, 0, tzinfo=timezone.utc)
    retry_at = parse_denial_reset_at(
        "You've hit your session limit · resets 2:50pm (UTC)", now=now
    )

    assert retry_at.date() == (now.date() + timedelta(days=1))


def test_parse_denial_reset_at_falls_back_to_utc_for_unknown_timezone():
    now = datetime(2026, 6, 26, 1, 0, 0, tzinfo=timezone.utc)
    retry_at = parse_denial_reset_at(
        "You've hit your weekly limit · resets 5:00am (Not/AZone)", now=now
    )

    assert retry_at is not None
    assert retry_at.tzinfo is not None


def test_parse_denial_reset_at_returns_none_for_unrelated_error():
    assert parse_denial_reset_at("agent process ended unexpectedly") is None


def test_parse_relative_duration_round_trips_with_format_duration():
    for delta in (timedelta(minutes=11), timedelta(hours=2, minutes=49), timedelta(days=4)):
        assert parse_relative_duration(format_duration(delta)) == timedelta(
            minutes=round(delta.total_seconds() / 60)
        )


def test_parse_relative_duration_returns_none_for_garbage():
    assert parse_relative_duration("not a duration") is None
    assert parse_relative_duration("") is None
