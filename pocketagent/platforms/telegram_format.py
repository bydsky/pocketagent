"""Convert agent markdown into Telegram's HTML message format.

Telegram shows messages sent without a parse_mode as literal text, so an
agent's markdown (``` fences, **bold**, `code`, tables, headings) arrives full
of raw symbols. Telegram's HTML mode only supports a small tag set (b, i, s,
u, code, pre, a, blockquote), so this maps markdown onto that subset:

- fenced code      -> <pre><code class="language-x">
- inline code      -> <code>
- **bold** / __b__ -> <b>, *italic* / _i_ -> <i>, ~~strike~~ -> <s>
- [text](url)      -> <a href>
- # headings       -> <b> lines
- > quotes         -> <blockquote> (GitHub "> [!NOTE]" alerts get a bold label)
- tables           -> a column-aligned <pre> block (Telegram has no tables)
- - / * bullets    -> "•", - [ ] / - [x] task items -> ☐ / ☑
- --- rules        -> a line of ─

HTML mode is preferred over MarkdownV2 because MarkdownV2 requires escaping
nearly every punctuation character, and any miss rejects the whole message.
Callers should still fall back to plain text if Telegram rejects the result.
"""

from __future__ import annotations

import html
import re
import unicodedata

_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([\w+#.-]*)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_RULE_RE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_TASK_RE = re.compile(r"^\[([ xX])\]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
_ALERT_RE = re.compile(r"^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*$", re.IGNORECASE)
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")

_CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__", re.DOTALL)
_ITALIC_RE = re.compile(
    r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])|(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])"
)
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_TOKEN_RE = re.compile("\x00(\\d+)\x00")


def markdown_to_telegram_html(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        fence = _FENCE_RE.match(line)
        if fence:
            marker, lang = fence.group(2), fence.group(3)
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker):
                body.append(lines[i])
                i += 1
            i += 1  # skip the closing fence (or run off the end if unclosed)
            code = html.escape("\n".join(body), quote=False)
            attr = f' class="language-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{attr}>{code}</code></pre>")
            continue

        if _is_table_start(lines, i):
            rows = [lines[i]]
            i += 2  # header + separator
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_render_table(rows))
            continue

        if _QUOTE_RE.match(line):
            quoted: list[str] = []
            while i < len(lines) and (m := _QUOTE_RE.match(lines[i])):
                quoted.append(m.group(1))
                i += 1
            alert = _ALERT_RE.match(quoted[0].strip()) if quoted else None
            if alert:
                quoted[0] = f"\x01{alert.group(1).capitalize()}:"
            rendered = [
                f"<b>{q[1:]}</b>" if q.startswith("\x01") else _inline(q) for q in quoted
            ]
            out.append("<blockquote>" + "\n".join(rendered).strip("\n") + "</blockquote>")
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            out.append(f"<b>{_inline(heading.group(1))}</b>")
        elif _RULE_RE.match(line):
            out.append("──────────")
        elif bullet := _BULLET_RE.match(line):
            indent, item = bullet.group(1), bullet.group(2)
            task = _TASK_RE.match(item)
            if task:
                mark = "☑" if task.group(1) in "xX" else "☐"
                out.append(f"{indent}{mark} {_inline(task.group(2))}")
            else:
                out.append(f"{indent}• {_inline(item)}")
        else:
            out.append(_inline(line))
        i += 1

    return "\n".join(out)


def _inline(text: str) -> str:
    """Escape one line of prose and apply inline markdown."""
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    # Code spans first so their contents are never treated as markdown.
    text = _CODE_SPAN_RE.sub(
        lambda m: keep(f"<code>{html.escape(m.group(2).strip(), quote=False)}</code>"), text
    )
    text = html.escape(text, quote=False)
    text = _LINK_RE.sub(
        lambda m: keep(f'<a href="{m.group(2).replace(chr(34), "&quot;")}">{m.group(1)}</a>'),
        text,
    )
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)
    text = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)
    # Tokens can nest (a link's text may hold a code span), so resolve repeatedly.
    while _TOKEN_RE.search(text):
        text = _TOKEN_RE.sub(lambda m: stash[int(m.group(1))], text)
    return text


def _is_table_start(lines: list[str], i: int) -> bool:
    return (
        i + 1 < len(lines)
        and lines[i].strip().startswith("|")
        and bool(_TABLE_SEP_RE.match(lines[i + 1]))
    )


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    cells = re.split(r"(?<!\\)\|", row)
    # Drop inline markup that would show up literally inside <pre>.
    return [re.sub(r"\*\*|__|`|~~", "", c.replace("\\|", "|")).strip() for c in cells]


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _render_table(rows: list[str]) -> str:
    table = [_cells(r) for r in rows]
    ncols = max(len(r) for r in table)
    table = [r + [""] * (ncols - len(r)) for r in table]
    widths = [max(_width(r[c]) for r in table) for c in range(ncols)]

    def fmt(row: list[str]) -> str:
        return " │ ".join(cell + " " * (widths[c] - _width(cell)) for c, cell in enumerate(row)).rstrip()

    lines = [fmt(table[0]), "─┼─".join("─" * w for w in widths)] + [fmt(r) for r in table[1:]]
    return "<pre>" + html.escape("\n".join(lines), quote=False) + "</pre>"
