from pocketagent.config import AppConfig, PlatformConfig, build_reset_groups, load_config
from pocketagent.core.commands import CommandRegistry
from pocketagent.core.router import ChannelOverride


def _config(daily_reset_time="", daily_reset_timezone="", **platforms_kwargs) -> AppConfig:
    """platforms_kwargs maps platform name -> {"channels": {...}, "options": {...}}."""

    platforms = {
        name: PlatformConfig(
            name=name,
            options=spec.get("options", {}),
            default_agent="claude_code",
            base_dir="/tmp",
            channels=spec.get("channels", {}),
        )
        for name, spec in platforms_kwargs.items()
    }
    return AppConfig(
        state_dir="/tmp",
        platforms=platforms,
        agent_options={},
        commands=CommandRegistry(),
        daily_reset_time=daily_reset_time,
        daily_reset_timezone=daily_reset_timezone,
    )


def test_load_config_parses_channel_daily_reset_time(tmp_path):
    config_path = tmp_path / "pocketagent.toml"
    config_path.write_text(
        """
        [daily_reset]
        time = "04:00"

        [platforms.discord]
        token = "x"
        default_agent = "claude_code"
        base_dir = "/tmp/ws"

        [platforms.discord.channels."111"]
        daily_reset_time = "12:00"
        daily_reset_timezone = "America/New_York"
        """
    )

    config = load_config(config_path)

    override = config.platforms["discord"].channels["111"]
    assert override.daily_reset_time == "12:00"
    assert override.daily_reset_timezone == "America/New_York"


def test_load_config_parses_platform_daily_reset_exclude_channels(tmp_path):
    config_path = tmp_path / "pocketagent.toml"
    config_path.write_text(
        """
        [daily_reset]
        time = "04:00"

        [platforms.discord]
        token = "x"
        default_agent = "claude_code"
        base_dir = "/tmp/ws"
        daily_reset_exclude_channels = ["111", "222"]
        """
    )

    config = load_config(config_path)

    assert config.platforms["discord"].options["daily_reset_exclude_channels"] == ["111", "222"]


def test_load_config_parses_scheduled_tasks(tmp_path):
    config_path = tmp_path / "pocketagent.toml"
    config_path.write_text(
        """
        [platforms.discord]
        token = "x"
        default_agent = "claude_code"
        base_dir = "/tmp/ws"
        """
    )
    (tmp_path / "scheduled_tasks.toml").write_text(
        """
        [[scheduled_tasks]]
        platform = "discord"
        channel_id = "111"
        user_id = "222"
        time = "21:00"
        timezone = "Australia/Sydney"
        prompt = "Summarize today's new vocabulary."
        """
    )

    config = load_config(config_path)

    assert len(config.scheduled_tasks) == 1
    task = config.scheduled_tasks[0]
    assert task.platform == "discord"
    assert task.channel_id == "111"
    assert task.user_id == "222"
    assert task.time == "21:00"
    assert task.timezone == "Australia/Sydney"
    assert task.prompt == "Summarize today's new vocabulary."


def test_load_config_scheduled_tasks_defaults_to_empty():
    config = _config()
    assert config.scheduled_tasks == []


def test_build_reset_groups_no_config_returns_nothing():
    config = _config()
    assert build_reset_groups(config) == []


def test_build_reset_groups_global_default_only():
    config = _config(daily_reset_time="04:00", daily_reset_timezone="UTC")
    groups = build_reset_groups(config)
    assert len(groups) == 1
    assert groups[0].time == "04:00"
    assert groups[0].channel_pairs is None
    assert groups[0].exclude == frozenset()


def test_build_reset_groups_channel_override_excluded_from_global():
    config = _config(
        daily_reset_time="04:00",
        discord={"channels": {"111": ChannelOverride(daily_reset_time="12:00")}},
    )
    groups = build_reset_groups(config)

    custom = next(g for g in groups if g.channel_pairs is not None)
    default = next(g for g in groups if g.channel_pairs is None)

    assert custom.time == "12:00"
    assert custom.channel_pairs == {("discord", "111")}
    assert default.exclude == frozenset({("discord", "111")})


def test_build_reset_groups_disabled_channel_excluded_with_no_custom_group():
    config = _config(
        daily_reset_time="04:00",
        discord={"channels": {"111": ChannelOverride(daily_reset_time="")}},
    )
    groups = build_reset_groups(config)

    assert len(groups) == 1  # no custom group for a disabled channel
    assert groups[0].channel_pairs is None
    assert groups[0].exclude == frozenset({("discord", "111")})


def test_build_reset_groups_daily_reset_exclude_channels_list():
    config = _config(
        daily_reset_time="04:00",
        discord={"options": {"daily_reset_exclude_channels": ["111", "222"]}},
    )
    groups = build_reset_groups(config)

    assert len(groups) == 1  # no custom group, just exclusions
    assert groups[0].channel_pairs is None
    assert groups[0].exclude == frozenset({("discord", "111"), ("discord", "222")})


def test_build_reset_groups_predicate_matches_only_assigned_channels():
    config = _config(
        daily_reset_time="04:00",
        discord={
            "channels": {
                "111": ChannelOverride(daily_reset_time="12:00"),
                "222": ChannelOverride(daily_reset_time=""),
            },
            "options": {"daily_reset_exclude_channels": ["333"]},
        },
    )
    groups = build_reset_groups(config)
    custom = next(g for g in groups if g.channel_pairs is not None)
    default = next(g for g in groups if g.channel_pairs is None)

    custom_predicate = custom.predicate()
    default_predicate = default.predicate()

    assert custom_predicate("discord:111:1") is True
    assert custom_predicate("discord:444:1") is False

    assert default_predicate("discord:444:1") is True  # uninvolved channel: default applies
    assert default_predicate("discord:111:1") is False  # has its own schedule
    assert default_predicate("discord:222:1") is False  # explicitly disabled (daily_reset_time="")
    assert default_predicate("discord:333:1") is False  # listed in daily_reset_exclude_channels
