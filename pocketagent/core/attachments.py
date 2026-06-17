"""Shared helper for persisting file/image attachments into a workspace dir."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .types import FileAttachment

ATTACHMENTS_DIRNAME = ".pocketagent/attachments"


def save_files(work_dir: str, files: Sequence[FileAttachment]) -> list[str]:
    """Write files into work_dir/ATTACHMENTS_DIRNAME and return their paths.

    File names are reduced to their basename, so a malicious or buggy
    `file_name` (e.g. "../../escape.txt") can't write outside the
    attachments dir.
    """

    if not files:
        return []
    attach_dir = Path(work_dir) / ATTACHMENTS_DIRNAME
    attach_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, f in enumerate(files):
        name = Path(f.file_name or f"file_{i}").name or f"file_{i}"
        path = attach_dir / name
        path.write_bytes(f.data)
        paths.append(str(path))
    return paths
