from pathlib import Path

import pytest

from video_extract.workspace import WorkspaceError, discover_workspace


CONFIG = """schema_version = 1
[paths]
project = "video-extract-core"
media = "media"
obsidian = "学习系统"
[obsidian]
generated = "资料库/网站视频转录"
threads = "threads"
concepts = "concepts"
review = "REVIEW.md"
"""


def write(root: Path) -> Path:
    path = root / "workspace.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG, encoding="utf-8")
    return path


def test_relative_paths_follow_workspace_when_moved(tmp_path: Path) -> None:
    first = tmp_path / "first"; config = discover_workspace(write(first))
    assert config.media == first / "media"
    second = tmp_path / "second"; first.rename(second)
    assert discover_workspace(second / "workspace.toml").media == second / "media"


def test_discovery_precedence(tmp_path: Path) -> None:
    explicit = write(tmp_path / "explicit")
    environment = write(tmp_path / "environment")
    ancestor = write(tmp_path / "ancestor")
    locator_target = write(tmp_path / "locator-target")
    locator = tmp_path / "locator.toml"; locator.write_text(f'workspace = "{locator_target}"\n', encoding="utf-8")
    assert discover_workspace(explicit, ancestor, {"VIDEO_EXTRACT_WORKSPACE": str(environment)}, locator).source == "explicit"
    assert discover_workspace(None, ancestor, {"VIDEO_EXTRACT_WORKSPACE": str(environment)}, locator).source == "environment"
    assert discover_workspace(None, ancestor / "nested", {}, locator).source == "ancestor"
    assert discover_workspace(None, tmp_path / "empty", {}, locator).source == "locator"


def test_containment_rejects_escape(tmp_path: Path) -> None:
    path = write(tmp_path)
    path.write_text(CONFIG.replace('media = "media"', 'media = "../media"'), encoding="utf-8")
    with pytest.raises(WorkspaceError, match="escapes"): discover_workspace(path)


def test_containment_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"; outside.mkdir()
    root = tmp_path / "root"; path = write(root)
    (root / "media").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="escapes"): discover_workspace(path)


def test_missing_config_is_clear_and_read_only(tmp_path: Path) -> None:
    before = list(tmp_path.iterdir())
    with pytest.raises(WorkspaceError, match="workspace.toml not found"): discover_workspace(None, tmp_path, {}, tmp_path / "missing.toml")
    assert list(tmp_path.iterdir()) == before
