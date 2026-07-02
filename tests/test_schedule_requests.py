from pocketagent.core.schedule_requests import (
    RemoveTaskRequest,
    ScheduleRequest,
    ScheduleRequestError,
    extract_list_task_requests,
    extract_remove_task_requests,
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
        'cron = "0 9 * * *"\n'
        'timezone = "America/New_York"\n'
        'prompt = "Check on the build."\n'
        "```"
    )
    cleaned, requests = extract_schedule_requests(text)
    assert cleaned == "Sure, I'll do that daily."
    assert requests == [
        ScheduleRequest(cron="0 9 * * *", prompt="Check on the build.", timezone="America/New_York")
    ]


def test_extract_block_without_timezone_defaults_empty():
    text = '```schedule-task\ncron = "0 9 * * *"\nprompt = "hi"\n```'
    cleaned, requests = extract_schedule_requests(text)
    assert cleaned == ""
    assert requests == [ScheduleRequest(cron="0 9 * * *", prompt="hi", timezone="")]


def test_extract_multiple_blocks_in_order():
    text = (
        '```schedule-task\ncron = "0 9 * * *"\nprompt = "one"\n```\n'
        '```schedule-task\ncron = "0 10 * * *"\nprompt = "two"\n```'
    )
    cleaned, requests = extract_schedule_requests(text)
    assert cleaned == ""
    assert requests == [
        ScheduleRequest(cron="0 9 * * *", prompt="one", timezone=""),
        ScheduleRequest(cron="0 10 * * *", prompt="two", timezone=""),
    ]


def test_extract_missing_prompt_returns_error():
    text = '```schedule-task\ncron = "0 9 * * *"\n```'
    cleaned, requests = extract_schedule_requests(text)
    assert cleaned == ""
    assert len(requests) == 1
    assert isinstance(requests[0], ScheduleRequestError)


def test_extract_missing_cron_returns_error():
    text = '```schedule-task\nprompt = "hi"\n```'
    cleaned, requests = extract_schedule_requests(text)
    assert isinstance(requests[0], ScheduleRequestError)


def test_extract_invalid_cron_returns_error():
    text = '```schedule-task\ncron = "not a cron expression"\nprompt = "hi"\n```'
    cleaned, requests = extract_schedule_requests(text)
    assert isinstance(requests[0], ScheduleRequestError)


def test_extract_invalid_toml_returns_error():
    text = "```schedule-task\nthis is not = = valid toml\n```"
    cleaned, requests = extract_schedule_requests(text)
    assert isinstance(requests[0], ScheduleRequestError)


def test_extract_non_string_cron_returns_error():
    text = '```schedule-task\ncron = 900\nprompt = "hi"\n```'
    cleaned, requests = extract_schedule_requests(text)
    assert isinstance(requests[0], ScheduleRequestError)


def test_extract_valid_biweekly_block():
    text = (
        '```schedule-task\ncron = "0 19 * * 4"\n'
        'interval_weeks = 2\nprompt = "check in"\n```'
    )
    cleaned, requests = extract_schedule_requests(text)
    assert requests == [ScheduleRequest(cron="0 19 * * 4", prompt="check in", interval_weeks=2)]


def test_extract_invalid_interval_weeks_returns_error():
    text = '```schedule-task\ncron = "0 19 * * 4"\ninterval_weeks = 0\nprompt = "hi"\n```'
    cleaned, requests = extract_schedule_requests(text)
    assert isinstance(requests[0], ScheduleRequestError)


def test_extract_list_no_block_returns_text_unchanged():
    cleaned, count = extract_list_task_requests("just a normal reply")
    assert cleaned == "just a normal reply"
    assert count == 0


def test_extract_list_block():
    text = "Sure, let me check.\n\n```list-scheduled-tasks\n```"
    cleaned, count = extract_list_task_requests(text)
    assert cleaned == "Sure, let me check."
    assert count == 1


def test_extract_list_block_without_trailing_newline():
    text = "```list-scheduled-tasks```"
    cleaned, count = extract_list_task_requests(text)
    assert cleaned == ""
    assert count == 1


def test_extract_multiple_list_blocks():
    text = "```list-scheduled-tasks\n```\n```list-scheduled-tasks\n```"
    cleaned, count = extract_list_task_requests(text)
    assert cleaned == ""
    assert count == 2


def test_extract_remove_no_block_returns_text_unchanged():
    cleaned, requests = extract_remove_task_requests("just a normal reply")
    assert cleaned == "just a normal reply"
    assert requests == []


def test_extract_remove_valid_block():
    text = 'Sure, cancelling that.\n\n```remove-schedule-task\nid = "abc123"\n```'
    cleaned, requests = extract_remove_task_requests(text)
    assert cleaned == "Sure, cancelling that."
    assert requests == [RemoveTaskRequest(id="abc123")]


def test_extract_remove_missing_id_returns_error():
    text = "```remove-schedule-task\n```"
    cleaned, requests = extract_remove_task_requests(text)
    assert isinstance(requests[0], ScheduleRequestError)


def test_extract_remove_non_string_id_returns_error():
    text = "```remove-schedule-task\nid = 123\n```"
    cleaned, requests = extract_remove_task_requests(text)
    assert isinstance(requests[0], ScheduleRequestError)


def test_extract_remove_invalid_toml_returns_error():
    text = "```remove-schedule-task\nthis is not = = valid toml\n```"
    cleaned, requests = extract_remove_task_requests(text)
    assert isinstance(requests[0], ScheduleRequestError)
