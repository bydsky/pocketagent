from pocketagent.core.schedule_requests import (
    ScheduleRequest,
    ScheduleRequestError,
    extract_schedule_requests,
)


def test_extract_no_block_returns_text_unchanged():
    cleaned, requests = extract_schedule_requests("just a normal reply")
    assert cleaned == "just a normal reply"
    assert requests == []


def test_extract_valid_block():
    text = (
        "Sure, I'll do that daily.\n\n"
        "```schedule-task\n"
        'time = "09:00"\n'
        'timezone = "America/New_York"\n'
        'prompt = "Check on the build."\n'
        "```"
    )
    cleaned, requests = extract_schedule_requests(text)
    assert cleaned == "Sure, I'll do that daily."
    assert requests == [
        ScheduleRequest(time="09:00", prompt="Check on the build.", timezone="America/New_York")
    ]


def test_extract_block_without_timezone_defaults_empty():
    text = '```schedule-task\ntime = "09:00"\nprompt = "hi"\n```'
    cleaned, requests = extract_schedule_requests(text)
    assert cleaned == ""
    assert requests == [ScheduleRequest(time="09:00", prompt="hi", timezone="")]


def test_extract_multiple_blocks_in_order():
    text = (
        '```schedule-task\ntime = "09:00"\nprompt = "one"\n```\n'
        '```schedule-task\ntime = "10:00"\nprompt = "two"\n```'
    )
    cleaned, requests = extract_schedule_requests(text)
    assert cleaned == ""
    assert requests == [
        ScheduleRequest(time="09:00", prompt="one", timezone=""),
        ScheduleRequest(time="10:00", prompt="two", timezone=""),
    ]


def test_extract_missing_required_field_returns_error():
    text = '```schedule-task\ntime = "09:00"\n```'
    cleaned, requests = extract_schedule_requests(text)
    assert cleaned == ""
    assert len(requests) == 1
    assert isinstance(requests[0], ScheduleRequestError)


def test_extract_invalid_time_returns_error():
    text = '```schedule-task\ntime = "25:99"\nprompt = "hi"\n```'
    cleaned, requests = extract_schedule_requests(text)
    assert isinstance(requests[0], ScheduleRequestError)
    assert "25:99" in requests[0].detail


def test_extract_invalid_toml_returns_error():
    text = "```schedule-task\nthis is not = = valid toml\n```"
    cleaned, requests = extract_schedule_requests(text)
    assert isinstance(requests[0], ScheduleRequestError)


def test_extract_non_string_time_returns_error():
    text = "```schedule-task\ntime = 900\nprompt = \"hi\"\n```"
    cleaned, requests = extract_schedule_requests(text)
    assert isinstance(requests[0], ScheduleRequestError)


def test_extract_valid_every_block():
    text = '```schedule-task\nevery = "2h"\nprompt = "Check on the build."\n```'
    cleaned, requests = extract_schedule_requests(text)
    assert cleaned == ""
    assert requests == [ScheduleRequest(every="2h", prompt="Check on the build.")]


def test_extract_every_and_time_together_returns_error():
    text = '```schedule-task\ntime = "09:00"\nevery = "2h"\nprompt = "hi"\n```'
    cleaned, requests = extract_schedule_requests(text)
    assert isinstance(requests[0], ScheduleRequestError)


def test_extract_neither_time_nor_every_returns_error():
    text = '```schedule-task\nprompt = "hi"\n```'
    cleaned, requests = extract_schedule_requests(text)
    assert isinstance(requests[0], ScheduleRequestError)


def test_extract_invalid_every_returns_error():
    text = '```schedule-task\nevery = "not-a-duration"\nprompt = "hi"\n```'
    cleaned, requests = extract_schedule_requests(text)
    assert isinstance(requests[0], ScheduleRequestError)
    assert "not-a-duration" in requests[0].detail


def test_extract_valid_weekly_block():
    text = '```schedule-task\ntime = "19:00"\nweekday = "thursday"\nprompt = "check in"\n```'
    cleaned, requests = extract_schedule_requests(text)
    assert cleaned == ""
    assert requests == [ScheduleRequest(time="19:00", prompt="check in", weekday="thursday")]


def test_extract_valid_biweekly_block():
    text = (
        '```schedule-task\ntime = "19:00"\nweekday = "thursday"\n'
        'interval_weeks = 2\nprompt = "check in"\n```'
    )
    cleaned, requests = extract_schedule_requests(text)
    assert requests == [
        ScheduleRequest(time="19:00", prompt="check in", weekday="thursday", interval_weeks=2)
    ]


def test_extract_unknown_weekday_returns_error():
    text = '```schedule-task\ntime = "19:00"\nweekday = "someday"\nprompt = "hi"\n```'
    cleaned, requests = extract_schedule_requests(text)
    assert isinstance(requests[0], ScheduleRequestError)


def test_extract_interval_weeks_without_weekday_returns_error():
    text = '```schedule-task\ntime = "19:00"\ninterval_weeks = 2\nprompt = "hi"\n```'
    cleaned, requests = extract_schedule_requests(text)
    assert isinstance(requests[0], ScheduleRequestError)


def test_extract_weekday_combined_with_every_returns_error():
    text = '```schedule-task\nevery = "2h"\nweekday = "thursday"\nprompt = "hi"\n```'
    cleaned, requests = extract_schedule_requests(text)
    assert isinstance(requests[0], ScheduleRequestError)
