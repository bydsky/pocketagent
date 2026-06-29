from datetime import timedelta

from pocketagent.core.utils import format_duration, parse_relative_duration


def test_parse_relative_duration_round_trips_with_format_duration():
    for delta in (timedelta(minutes=11), timedelta(hours=2, minutes=49), timedelta(days=4)):
        assert parse_relative_duration(format_duration(delta)) == timedelta(
            minutes=round(delta.total_seconds() / 60)
        )


def test_parse_relative_duration_returns_none_for_garbage():
    assert parse_relative_duration("not a duration") is None
    assert parse_relative_duration("") is None
