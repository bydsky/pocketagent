"""Clean up agent replies for chat platforms.

Claude Code's MessageDisplay hooks (e.g. the bbilingual plugin) rewrite the
assistant text that stream-json reports, not just what the TUI draws. bbilingual
puts a translated line under each English line, and with BBILINGUAL_STYLE set
it wraps each translation in ANSI color codes (``\\x1b[90m...\\x1b[39m``).
Chat apps show those escape codes as junk.

`clean_reply` strips all ANSI escapes and turns each translation line into a
markdown quote (``> 译文``). Every platform here shows ``>`` as a quote, either
natively (Discord, Slack) or via the Telegram HTML converter.

A line counts as a translation when bbilingual colored it (any ANSI code in
it, even if code spans and URLs make it mostly Latin), or, without colors, when
it's mostly CJK outside code spans and URLs and comes right after an English
prose line. Headings, which bbilingual writes as ``# English / 中文``, get split into
the heading plus a quote line. Code fences and table rows are left alone; a
translated table stays a separate table under the original.
"""

from __future__ import annotations

import re

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CJK_RE = re.compile(r"[　-ヿ㐀-鿿가-힯＀-￯]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^(\s{0,3}#{1,6}\s+)(.*?)\s+/\s+(.*)$")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _mostly_cjk(line: str) -> bool:
    # Same rule bbilingual itself uses for "already in the target script".
    cjk = len(_CJK_RE.findall(line))
    return cjk > 0 and cjk * 3 >= len(re.findall(r"[A-Za-z]", line))


_CODE_SPAN_RE = re.compile(r"`[^`]*`")
_URL_RE = re.compile(r"https?://\S+")


def _mostly_cjk_prose(line: str) -> bool:
    # Like _mostly_cjk, but code spans and URLs don't count: a translation such as
    # "已提交为 `0c9fa0d`，PR 已更新：https://github.com/..." is Chinese prose even
    # though most of its letters are Latin.
    return _mostly_cjk(_URL_RE.sub("", _CODE_SPAN_RE.sub("", line)))


def _is_english_prose(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith(("|", ">")) and not _mostly_cjk(s) and bool(_LATIN_WORD_RE.search(s))


def _as_quote(line: str) -> str:
    # Always at column 0: an indented ">" (under a list item) isn't reliably
    # treated as a quote by chat apps.
    return f"> {line.strip()}"


def clean_reply(text: str) -> str:
    if "\x1b" not in text and not _CJK_RE.search(text):
        return text

    out: list[str] = []
    translated: set[int] = set()
    fence: str | None = None
    prev_clean = ""
    for raw in text.split("\n"):
        line = strip_ansi(raw)
        stripped = line.strip()

        fence_match = _FENCE_RE.match(line)
        if fence is not None:
            if fence_match and stripped.startswith(fence):
                fence = None
            out.append(line)
            prev_clean = ""
            continue
        if fence_match:
            fence = fence_match.group(1)
            out.append(line)
            prev_clean = ""
            continue

        heading = _HEADING_RE.match(line)
        if heading and _mostly_cjk(heading.group(3)) and not _mostly_cjk(heading.group(2)):
            out.append(heading.group(1) + heading.group(2))
            translated.add(len(out))
            out.append(_as_quote(heading.group(3)))
            prev_clean = ""
            continue

        # Only bbilingual puts ANSI colors in the agent's text, so a colored line is a
        # translation however much Latin it holds (code spans, URLs, a quoted English
        # title). It colors just the plain-text pieces, leaving code spans and markers.
        colored = raw != line
        is_translation = bool(stripped) and not stripped.startswith("|") and (
            colored or (_mostly_cjk_prose(stripped) and _is_english_prose(prev_clean))
        )
        if is_translation:
            translated.add(len(out))
            out.append(_as_quote(line))
            prev_clean = ""
        else:
            out.append(line)
            prev_clean = line

    # A translation quote right next to one of the reply's own ">" quotes would
    # merge into a single quote block; separate them with a blank line.
    result: list[str] = []
    for i, line in enumerate(out):
        if i and (i in translated) != (i - 1 in translated) and _is_quote(line) and _is_quote(out[i - 1]):
            result.append("")
        result.append(line)
    return "\n".join(result)


def _is_quote(line: str) -> bool:
    return line.lstrip().startswith(">")
