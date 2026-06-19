from pocketagent.core.workspace import WorkspaceManager, sanitize_folder_name


def test_sanitize_folder_name_basic():
    assert sanitize_folder_name("general") == "general"


def test_sanitize_folder_name_strips_unsafe_chars():
    assert sanitize_folder_name("support / bot!! #1") == "support-bot-1"


def test_sanitize_folder_name_empty_falls_back():
    assert sanitize_folder_name("   ") == "channel"


def test_resolve_dir_creates_directory(tmp_path):
    manager = WorkspaceManager(tmp_path / "base")
    path = manager.resolve_dir("123", "general")
    assert path.is_dir()
    assert path == tmp_path / "base" / "general"


def test_resolve_dir_persists_binding_across_instances(tmp_path):
    base = tmp_path / "base"
    first = WorkspaceManager(base)
    path1 = first.resolve_dir("123", "general")

    second = WorkspaceManager(base)
    path2 = second.resolve_dir("123", "renamed-channel")

    assert path1 == path2


def test_resolve_dir_disambiguates_name_collision(tmp_path):
    manager = WorkspaceManager(tmp_path / "base")
    path1 = manager.resolve_dir("111", "general")
    path2 = manager.resolve_dir("222", "general")
    assert path1 != path2
    assert path2.name == "general-2"


def test_resolve_dir_falls_back_to_channel_key(tmp_path):
    manager = WorkspaceManager(tmp_path / "base")
    path = manager.resolve_dir("999", None)
    assert path.name == "999"


def test_resolve_dir_absolute_path_override_bypasses_base_dir(tmp_path):
    target = tmp_path / "elsewhere" / "project"
    manager = WorkspaceManager(tmp_path / "base")
    path = manager.resolve_dir("123", str(target))
    assert path == target
    assert path.is_dir()


def test_resolve_dir_expands_user_in_absolute_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    manager = WorkspaceManager(tmp_path / "base")
    path = manager.resolve_dir("123", "~/workspace/pocketagent")
    assert path == tmp_path / "workspace" / "pocketagent"
    assert path.is_dir()


def test_resolve_dir_absolute_override_persists_across_instances(tmp_path):
    target = tmp_path / "elsewhere" / "project"
    base = tmp_path / "base"
    first = WorkspaceManager(base)
    path1 = first.resolve_dir("123", str(target))

    second = WorkspaceManager(base)
    path2 = second.resolve_dir("123", "some-other-name")

    assert path1 == path2 == target
