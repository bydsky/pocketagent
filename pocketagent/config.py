"""Loads pocketagent.toml and builds the wired-up runtime objects."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .agents.claude_code import ClaudeCodeAgent
from .agents.codex import CodexAgent
from .agents.tmux import DEFAULT_PROMPT_PATTERN, TmuxAgent
from .core.agent import Agent
from .core.commands import CommandRegistry, CustomCommand
from .core.engine import Engine
from .core.platform import Platform
from .core.router import ChannelOverride, Router
from .core.session_store import SessionStore
from .core.workspace import WorkspaceManager


@dataclass
class PlatformConfig:
    name: str
    options: dict[str, Any]
    default_agent: str
    base_dir: str
    channels: dict[str, ChannelOverride] = field(default_factory=dict)


@dataclass
class AppConfig:
    state_dir: str
    platforms: dict[str, PlatformConfig]
    agent_options: dict[str, dict[str, Any]]
    commands: CommandRegistry


def load_config(path: str | Path) -> AppConfig:
    data = tomllib.loads(Path(path).read_text())

    state_dir = str(Path(data.get("state_dir", "~/.pocketagent")).expanduser())

    platforms: dict[str, PlatformConfig] = {}
    for name, raw in data.get("platforms", {}).items():
        if "base_dir" not in raw:
            raise ValueError(f"platforms.{name}: base_dir is required")
        if "default_agent" not in raw:
            raise ValueError(f"platforms.{name}: default_agent is required")
        channels = {
            str(channel_id): ChannelOverride(
                agent=channel_cfg.get("agent"),
                workspace=channel_cfg.get("workspace"),
                show_footer=channel_cfg.get("show_footer"),
            )
            for channel_id, channel_cfg in raw.get("channels", {}).items()
        }
        options = {k: v for k, v in raw.items() if k not in ("channels", "default_agent", "base_dir")}
        platforms[name] = PlatformConfig(
            name=name,
            options=options,
            default_agent=raw["default_agent"],
            base_dir=str(Path(raw["base_dir"]).expanduser()),
            channels=channels,
        )

    commands = CommandRegistry()
    for name, raw in data.get("commands", {}).items():
        commands.add(
            CustomCommand(
                name=name,
                prompt=raw.get("prompt"),
                exec=raw.get("exec"),
                description=raw.get("description", ""),
            )
        )

    return AppConfig(
        state_dir=state_dir,
        platforms=platforms,
        agent_options=data.get("agents", {}),
        commands=commands,
    )


AgentFactory = Callable[[dict[str, Any]], Agent]

AGENT_FACTORIES: dict[str, AgentFactory] = {
    "claude_code": lambda opts: ClaudeCodeAgent(
        command=opts.get("command", "claude"),
        model=opts.get("model", ""),
        permission_mode=opts.get("permission_mode", "default"),
        effort=opts.get("effort", ""),
        extra_args=opts.get("extra_args", []),
        agent_system_prompt=opts.get("agent_system_prompt", ""),
    ),
    "codex": lambda opts: CodexAgent(
        command=opts.get("command", "codex"),
        model=opts.get("model", ""),
        sandbox=opts.get("sandbox", "workspace-write"),
        ask_for_approval=opts.get("ask_for_approval", "never"),
        extra_args=opts.get("extra_args", []),
        agent_system_prompt=opts.get("agent_system_prompt", ""),
        skip_git_repo_check=opts.get("skip_git_repo_check", True),
    ),
    "tmux": lambda opts: TmuxAgent(
        session=opts.get("session", ""),
        pane=opts.get("pane", "0"),
        auto_create=opts.get("auto_create", True),
        shell=opts.get("shell", ""),
        init_command=opts.get("init_command", ""),
        startup_wait_ms=opts.get("startup_wait_ms", 0),
        prompt_pattern=opts.get("prompt_pattern", DEFAULT_PROMPT_PATTERN),
        poll_interval_ms=opts.get("poll_interval_ms", 200),
        strip_input_block=opts.get("strip_input_block", True),
        strip_patterns=opts.get("strip_patterns"),
        agent_system_prompt=opts.get("agent_system_prompt", ""),
    ),
}


def build_agents(agent_options: dict[str, dict[str, Any]]) -> dict[str, Agent]:
    agents: dict[str, Agent] = {}
    for name, opts in agent_options.items():
        kind = opts.get("type", name)
        factory = AGENT_FACTORIES.get(kind)
        if factory is None:
            raise ValueError(f"agents.{name}: unknown agent type '{kind}'")
        agents[name] = factory(opts)
    return agents


def build_discord_platform(cfg: PlatformConfig, commands: CommandRegistry) -> Platform:
    from .platforms.discord_platform import DiscordPlatform

    return DiscordPlatform(
        token=cfg.options.get("token", ""),
        allow_from=cfg.options.get("allow_from", ""),
        group_reply_all_guilds=cfg.options.get("group_reply_all_guilds", ""),
        require_mention_channels=cfg.options.get("require_mention_channels", ""),
        commands=commands,
    )


def build_telegram_platform(cfg: PlatformConfig, commands: CommandRegistry) -> Platform:
    from .platforms.telegram_platform import TelegramPlatform

    return TelegramPlatform(
        token=cfg.options.get("token", ""),
        allow_from=cfg.options.get("allow_from", ""),
        group_reply_all_chats=cfg.options.get("group_reply_all_chats", ""),
        require_mention_chats=cfg.options.get("require_mention_chats", ""),
        commands=commands,
    )


def build_slack_platform(cfg: PlatformConfig, commands: CommandRegistry) -> Platform:
    from .platforms.slack_platform import SlackPlatform

    return SlackPlatform(
        bot_token=cfg.options.get("bot_token", ""),
        app_token=cfg.options.get("app_token", ""),
        allow_from=cfg.options.get("allow_from", ""),
        group_reply_all_channels=cfg.options.get("group_reply_all_channels", ""),
        require_mention_channels=cfg.options.get("require_mention_channels", ""),
        commands=commands,
    )


PLATFORM_FACTORIES: dict[str, Callable[[PlatformConfig, CommandRegistry], Platform]] = {
    "discord": build_discord_platform,
    "telegram": build_telegram_platform,
    "slack": build_slack_platform,
}


def build_app(config: AppConfig) -> tuple[dict[str, Platform], Engine]:
    """Build platform instances and the Engine that wires them to agents."""

    agents = build_agents(config.agent_options)

    platforms: dict[str, Platform] = {}
    routers: dict[str, Router] = {}
    for name, platform_cfg in config.platforms.items():
        factory = PLATFORM_FACTORIES.get(name)
        if factory is None:
            raise ValueError(f"platforms.{name}: unknown platform type '{name}'")
        platforms[name] = factory(platform_cfg, config.commands)

        workspace = WorkspaceManager(platform_cfg.base_dir)
        routers[name] = Router(
            default_agent=platform_cfg.default_agent,
            workspace=workspace,
            channels=platform_cfg.channels,
            platform_system_prompt=platform_cfg.options.get("platform_system_prompt", ""),
            show_footer=platform_cfg.options.get("show_footer", False),
        )

    session_store = SessionStore(Path(config.state_dir) / "sessions.json")
    engine = Engine(agents=agents, routers=routers, session_store=session_store, commands=config.commands)
    return platforms, engine
