from pocketagent.core.commands import (
    CommandRegistry,
    CustomCommand,
    expand_prompt,
    parse_command_text,
)


def test_parse_command_text_basic():
    assert parse_command_text("/deploy api prod") == ("deploy", ["api", "prod"])


def test_parse_command_text_quoted_args():
    assert parse_command_text('/deploy "my api" prod') == ("deploy", ["my api", "prod"])


def test_parse_command_text_not_a_command():
    assert parse_command_text("hello world") is None


def test_parse_command_text_empty_after_slash():
    assert parse_command_text("/") is None


def test_expand_prompt_positional():
    assert expand_prompt("Deploy {{1}} now", ["api"]) == "Deploy api now"


def test_expand_prompt_positional_missing():
    assert expand_prompt("Deploy {{1}} and {{2}}", ["api"]) == "Deploy api and "


def test_expand_prompt_default():
    assert (
        expand_prompt("Deploy {{1}} to {{2:staging}}", ["api"])
        == "Deploy api to staging"
    )
    assert (
        expand_prompt("Deploy {{1}} to {{2:staging}}", ["api", "prod"])
        == "Deploy api to prod"
    )


def test_expand_prompt_tail_star():
    assert expand_prompt("Run: {{2*}}", ["x", "a", "b", "c"]) == "Run: a b c"
    assert expand_prompt("Run: {{1*}}", ["a", "b", "c"]) == "Run: a b c"


def test_expand_prompt_args_placeholder():
    assert expand_prompt("Do: {{args}}", ["a", "b"]) == "Do: a b"


def test_expand_prompt_no_placeholders_appends_args():
    assert expand_prompt("Run tests", ["-v"]) == "Run tests -v"
    assert expand_prompt("Run tests", []) == "Run tests"


def test_custom_command_requires_prompt_or_exec():
    import pytest

    with pytest.raises(ValueError):
        CustomCommand(name="bad")


def test_custom_command_rejects_both_prompt_and_exec():
    import pytest

    with pytest.raises(ValueError):
        CustomCommand(name="bad", prompt="x", exec="y")


def test_registry_expand_prompt_command():
    registry = CommandRegistry()
    registry.add(
        CustomCommand(
            name="deploy",
            prompt="Deploy {{1}} to the {{2:staging}} environment and report status.",
        )
    )
    result = registry.expand("/deploy api prod")
    assert result is not None
    cmd, expanded = result
    assert cmd.name == "deploy"
    assert expanded == "Deploy api to the prod environment and report status."


def test_registry_expand_exec_command():
    registry = CommandRegistry()
    registry.add(CustomCommand(name="status", exec="git status"))
    result = registry.expand("/status")
    assert result is not None
    cmd, expanded = result
    assert cmd.exec == "git status"
    assert expanded == "git status"


def test_registry_expand_exec_command_with_args():
    registry = CommandRegistry()
    registry.add(CustomCommand(name="status", exec="git status"))
    result = registry.expand("/status -s")
    assert result is not None
    _, expanded = result
    assert expanded == "git status -s"


def test_registry_expand_unknown_command_returns_none():
    registry = CommandRegistry()
    assert registry.expand("/nope") is None


def test_registry_expand_non_command_returns_none():
    registry = CommandRegistry()
    registry.add(CustomCommand(name="deploy", prompt="x"))
    assert registry.expand("just chatting") is None


def test_custom_command_allows_empty_exec_as_passthrough():
    # exec="" is a deliberate value (e.g. /shell with no fixed prefix), not
    # the same as exec unset -- must not raise.
    CustomCommand(name="shell", exec="")


def test_registry_expand_empty_exec_passes_through_full_args():
    registry = CommandRegistry()
    registry.add(CustomCommand(name="shell", exec=""))
    result = registry.expand("/shell ls -la")
    assert result is not None
    _, expanded = result
    assert expanded == "ls -la"


def test_registry_expand_empty_exec_with_no_args():
    registry = CommandRegistry()
    registry.add(CustomCommand(name="shell", exec=""))
    result = registry.expand("/shell")
    assert result is not None
    _, expanded = result
    assert expanded == ""


def test_registry_expand_empty_exec_preserves_quoting_verbatim():
    # exec="" must NOT round-trip through shlex.split()/' '.join(), which
    # would silently flatten a quoted multi-word argument into bare words.
    registry = CommandRegistry()
    registry.add(CustomCommand(name="shell", exec=""))
    result = registry.expand('/shell grep "foo bar" file.txt')
    assert result is not None
    _, expanded = result
    assert expanded == 'grep "foo bar" file.txt'


def test_registry_expand_empty_exec_collapses_only_boundary_whitespace():
    registry = CommandRegistry()
    registry.add(CustomCommand(name="shell", exec=""))
    result = registry.expand("/shell   ls   -la  ")
    assert result is not None
    _, expanded = result
    assert expanded == "ls   -la"
