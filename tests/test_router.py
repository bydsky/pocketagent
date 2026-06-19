from pathlib import Path

from pocketagent.core.router import ChannelOverride, Router
from pocketagent.core.workspace import WorkspaceManager


def make_router(tmp_path: Path, channels=None) -> Router:
    workspace = WorkspaceManager(tmp_path / "base")
    return Router(default_agent="claude_code", workspace=workspace, channels=channels or {})


def test_resolve_uses_default_agent_and_channel_name(tmp_path):
    router = make_router(tmp_path)
    route = router.resolve("111", "general")
    assert route.agent_name == "claude_code"
    assert route.work_dir == tmp_path / "base" / "general"
    assert route.work_dir.is_dir()


def test_resolve_falls_back_to_channel_id_when_no_name(tmp_path):
    router = make_router(tmp_path)
    route = router.resolve("222", "")
    assert route.work_dir == tmp_path / "base" / "222"


def test_resolve_channel_override_agent(tmp_path):
    router = make_router(
        tmp_path, channels={"111": ChannelOverride(agent="gemini")}
    )
    route = router.resolve("111", "general")
    assert route.agent_name == "gemini"
    assert route.work_dir == tmp_path / "base" / "general"


def test_resolve_channel_override_workspace(tmp_path):
    router = make_router(
        tmp_path, channels={"111": ChannelOverride(workspace="support-bot")}
    )
    route = router.resolve("111", "general")
    assert route.agent_name == "claude_code"
    assert route.work_dir == tmp_path / "base" / "support-bot"


def test_resolve_is_stable_across_calls(tmp_path):
    router = make_router(tmp_path)
    first = router.resolve("111", "general")
    second = router.resolve("111", "general")
    assert first.work_dir == second.work_dir


def test_resolve_keeps_binding_even_if_channel_renamed(tmp_path):
    router = make_router(tmp_path)
    first = router.resolve("111", "old-name")
    second = router.resolve("111", "new-name")
    assert first.work_dir == second.work_dir


def test_resolve_show_footer_defaults_false(tmp_path):
    router = make_router(tmp_path)
    assert router.resolve("111", "general").show_footer is False


def test_resolve_show_footer_platform_default_true(tmp_path):
    workspace = WorkspaceManager(tmp_path / "base")
    router = Router(default_agent="claude_code", workspace=workspace, show_footer=True)
    assert router.resolve("111", "general").show_footer is True


def test_resolve_show_footer_channel_override_wins(tmp_path):
    workspace = WorkspaceManager(tmp_path / "base")
    router = Router(
        default_agent="claude_code",
        workspace=workspace,
        channels={"111": ChannelOverride(show_footer=True)},
        show_footer=False,
    )
    assert router.resolve("111", "general").show_footer is True
    assert router.resolve("222", "general").show_footer is False
