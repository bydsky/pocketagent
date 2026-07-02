"""Custom slash commands: config-defined prompt templates or shell commands,
plus a handful of built-in commands Engine registers itself (e.g. /scheduled,
/unschedule -- see Engine._register_builtin_commands in core/engine.py).

A prompt/exec command is declared once in config (name + a prompt template,
or a shell `exec` string) and expanded against the user's typed arguments
into a normal prompt that gets sent to the agent like any other message --
there is no separate command-handler dispatch layer for those two kinds. A
`builtin` command instead carries an opaque tag Engine switches on to run
its own Python logic (not user-configurable; Engine only registers one if
the user hasn't already defined a command of that same name).

Placeholder syntax in `prompt` templates:
    {{1}}, {{2}}, ...   1-based positional argument
    {{N:default}}       positional argument N, or `default` if not supplied
    {{N*}}              all arguments from position N to the end, space-joined
    {{args}}            all arguments, space-joined
If a template contains no placeholders at all, the user's arguments are
appended to the end of the template instead.

An `exec` command's args are likewise appended (never template-expanded);
exec="" is a deliberate passthrough -- e.g. a "/shell" command with no fixed
prefix, where the user's full typed text becomes the entire shell command.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

_PLACEHOLDER_RE = re.compile(
    r"\{\{(?:(?P<star>\d+)\*|(?P<idx_def>\d+):(?P<default>[^}]*)|(?P<idx>\d+)|(?P<args>args))\}\}"
)


@dataclass
class CustomCommand:
    name: str
    prompt: str | None = None
    exec: str | None = None
    work_dir: str | None = None
    description: str = ""
    # Opaque tag for a built-in command Engine implements itself (e.g.
    # "list_scheduled_tasks"), as opposed to a user-configured prompt/exec.
    # Never set from user config -- see core/engine.py.
    builtin: str | None = None

    def __post_init__(self) -> None:
        # exec="" is a deliberate, valid value (e.g. a /shell passthrough
        # command with no fixed prefix) -- distinct from exec unset (None) --
        # so these checks use "is None", not truthiness.
        kinds = [bool(self.prompt), self.exec is not None, self.builtin is not None]
        if not any(kinds):
            raise ValueError(f"command {self.name!r} needs one of prompt, exec, or builtin")
        if sum(kinds) > 1:
            raise ValueError(f"command {self.name!r} can only set one of prompt, exec, or builtin")


def expand_prompt(template: str, args: list[str]) -> str:
    """Expand a command prompt template against positional args."""

    if "{{" not in template:
        if args:
            return f"{template} {' '.join(args)}"
        return template

    def replace(m: re.Match[str]) -> str:
        if m.group("star") is not None:
            start = int(m.group("star"))
            return " ".join(args[start - 1 :]) if start <= len(args) else ""
        if m.group("idx_def") is not None:
            idx = int(m.group("idx_def"))
            if idx <= len(args) and args[idx - 1] != "":
                return args[idx - 1]
            return m.group("default")
        if m.group("idx") is not None:
            idx = int(m.group("idx"))
            return args[idx - 1] if idx <= len(args) else ""
        if m.group("args") is not None:
            return " ".join(args)
        return m.group(0)

    return _PLACEHOLDER_RE.sub(replace, template)


def parse_command_text(content: str) -> tuple[str, list[str]] | None:
    """Parse "/name arg1 arg2" into (name, [arg1, arg2]). None if not a command."""

    content = content.strip()
    if not content.startswith("/"):
        return None
    try:
        parts = shlex.split(content[1:])
    except ValueError:
        parts = content[1:].split()
    if not parts:
        return None
    return parts[0], parts[1:]


def _raw_command_args(content: str) -> str:
    """Return the text after "/name", verbatim (no shlex split/rejoin).

    Used for exec="" passthrough commands so quoting in the user's original
    text (e.g. a quoted multi-word argument) reaches the shell unchanged.
    """

    body = content.strip()[1:]
    _, _, rest = body.partition(" ")
    return rest.strip()


class CommandRegistry:
    """Holds the set of configured custom commands."""

    def __init__(self) -> None:
        self._commands: dict[str, CustomCommand] = {}

    def add(self, command: CustomCommand) -> None:
        self._commands[command.name] = command

    def resolve(self, name: str) -> CustomCommand | None:
        return self._commands.get(name)

    def names(self) -> list[str]:
        return list(self._commands.keys())

    def all(self) -> list[CustomCommand]:
        return list(self._commands.values())

    def expand(self, content: str) -> tuple[CustomCommand, str] | None:
        """If content is a known command, return (command, expanded_prompt_or_exec).

        For prompt-based commands the second element is the expanded prompt to
        send to the agent. For exec-based commands it is the literal shell
        command (args are not template-expanded for exec; they're appended).
        An exec="" passthrough command (e.g. "/shell") uses the raw typed text
        verbatim instead of args re-joined with single spaces, so quoting
        (e.g. `/shell grep "foo bar" file.txt`) survives into the shell
        command unchanged -- shlex.split()/' '.join() round-tripping would
        otherwise silently flatten "foo bar" into two bare words. For a
        builtin command the second element is just the args re-joined with
        single spaces -- Engine re-splits them itself if it needs them
        individually (see Engine._handle_builtin_command).
        Returns None if content isn't a registered command.
        """

        parsed = parse_command_text(content)
        if parsed is None:
            return None
        name, args = parsed
        cmd = self.resolve(name)
        if cmd is None:
            return None
        if cmd.builtin is not None:
            return cmd, " ".join(args)
        if cmd.prompt is not None:
            return cmd, expand_prompt(cmd.prompt, args)
        assert cmd.exec is not None
        if cmd.exec == "":
            return cmd, _raw_command_args(content)
        if args:
            return cmd, f"{cmd.exec} {' '.join(args)}"
        return cmd, cmd.exec
