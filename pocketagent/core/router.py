"""Resolves which agent and workspace folder handle a given channel.

A platform configures one default_agent plus optional per-channel-id
overrides (agent and/or a fixed workspace folder name). Channels without an
override fall back to the default agent and a workspace folder derived from
the channel's display name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .workspace import WorkspaceManager


@dataclass
class ChannelOverride:
    agent: str | None = None
    workspace: str | None = None


@dataclass
class ResolvedRoute:
    agent_name: str
    work_dir: Path


class Router:
    def __init__(
        self,
        default_agent: str,
        workspace: WorkspaceManager,
        channels: dict[str, ChannelOverride] | None = None,
    ) -> None:
        self.default_agent = default_agent
        self.workspace = workspace
        self.channels = channels or {}

    def resolve(self, channel_id: str, channel_name: str = "") -> ResolvedRoute:
        override = self.channels.get(channel_id)
        agent_name = (override.agent if override else None) or self.default_agent
        preferred_workspace_name = (
            override.workspace if override else None
        ) or channel_name or channel_id
        work_dir = self.workspace.resolve_dir(channel_id, preferred_workspace_name)
        return ResolvedRoute(agent_name=agent_name, work_dir=work_dir)
