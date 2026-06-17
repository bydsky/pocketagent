"""Splits long messages into platform-sized chunks without breaking fenced
code blocks across a split (an unterminated ``` fence renders badly once
split across two separate chat messages)."""

from __future__ import annotations

_FENCE_PREFIX = "```"


def split_message(content: str, max_len: int) -> list[str]:
    if len(content) <= max_len:
        return [content] if content else []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    fence_lang: str | None = None  # set while inside an open ``` fence

    def current_text() -> str:
        return "\n".join(current)

    def flush() -> None:
        nonlocal current, current_len
        if not current:
            return
        text = current_text()
        if fence_lang is not None:
            text += "\n```"
        chunks.append(text)
        current = []
        current_len = 0

    lines = content.split("\n")
    for line in lines:
        stripped = line.strip()
        is_fence_marker = stripped.startswith(_FENCE_PREFIX)
        line_len = len(line) + 1  # account for the newline joiner

        if current and current_len + line_len > max_len:
            flush()
            if fence_lang is not None:
                current.append(f"```{fence_lang}")
                current_len += len(fence_lang) + 4

        current.append(line)
        current_len += line_len

        if is_fence_marker:
            if fence_lang is None:
                fence_lang = stripped[len(_FENCE_PREFIX):].strip()
            else:
                fence_lang = None

    flush()
    return chunks
