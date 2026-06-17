"""Per-channel workspace folders under a configured base directory.

Each channel gets its own folder under `base_dir`, named after the channel
(sanitized) unless a config override is supplied. The channel -> folder name
mapping is persisted as JSON next to base_dir so that if the channel's
display name later changes, the bot keeps using the same folder instead of
silently starting a fresh workspace.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_folder_name(name: str) -> str:
    """Reduce an arbitrary channel name/id to a safe directory name."""

    name = name.strip()
    safe = _UNSAFE_CHARS_RE.sub("-", name).strip("-._")
    return safe or "channel"


class WorkspaceManager:
    """Resolves and persists channel -> workspace folder bindings for one platform."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self._bindings_path = self.base_dir / ".pocketagent-bindings.json"
        self._lock = threading.Lock()
        self._bindings: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self._bindings_path.exists():
            try:
                self._bindings = json.loads(self._bindings_path.read_text())
            except (json.JSONDecodeError, OSError):
                self._bindings = {}

    def _save(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._bindings_path.write_text(json.dumps(self._bindings, indent=2))

    def resolve_dir(self, channel_key: str, preferred_name: str | None = None) -> Path:
        """Return the (created) workspace directory for channel_key.

        If channel_key was already bound, reuse that folder regardless of
        preferred_name. Otherwise bind to a sanitized version of
        preferred_name (or channel_key itself if no name is available),
        disambiguating on collision.
        """

        with self._lock:
            existing = self._bindings.get(channel_key)
            if existing:
                folder_name = existing
            else:
                folder_name = self._allocate_folder_name(
                    preferred_name or channel_key
                )
                self._bindings[channel_key] = folder_name
                self._save()

        path = self.base_dir / folder_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _allocate_folder_name(self, preferred_name: str) -> str:
        base = sanitize_folder_name(preferred_name)
        taken = set(self._bindings.values())
        if base not in taken:
            return base
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"
