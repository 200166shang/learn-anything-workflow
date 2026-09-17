"""T23 user launchd scheduling and unique-host boundaries."""

import plistlib
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from test_review_workflow import explained_question
from video_extract.delivery_schedule import install, remove, status


class Launchctl:
    def __init__(self) -> None:
        self.calls = []
        self.loaded = False

    def __call__(self, command):
        self.calls.append(list(command))
        if command[1] == "bootstrap":
            self.loaded = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[1] == "bootout":
            self.loaded = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0 if self.loaded else 113, stdout="", stderr="not found")


def executable(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "video-extract"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def enable_delivery(config, target: str) -> None:
    path = config.local / "delivery/config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabled": True, "readiness": {
        "target_fingerprint": hashlib.sha256(target.encode()).hexdigest()[:16]
    }}), encoding="utf-8")


def test_install_uses_only_public_tick_and_local_secret(tmp_path: Path) -> None:
    config, _, _ = explained_question(tmp_path)
    launchctl = Launchctl()
    enable_delivery(config, "private-chat")
    result = install(config, home=tmp_path / "home", executable=executable(tmp_path), runner=launchctl,
                     environment={"VIDEO_EXTRACT_LARK_CHAT_ID": "private-chat"}, hostname="primary")

    assert result["status"] == "completed"
    plist_path = next((tmp_path / "home/Library/LaunchAgents").glob("*.plist"))
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["RunAtLoad"] is True and plist["StartCalendarInterval"] == [{}]
    assert plist["ProgramArguments"][1:] == ["delivery", "tick", "--workspace", str(config.config_path), "--json"]
    assert plist["EnvironmentVariables"]["VIDEO_EXTRACT_LARK_CHAT_ID"] == "private-chat"
    assert str(Path(plist["ProgramArguments"][0]).parent) in plist["EnvironmentVariables"]["PATH"]
    assert "private-chat" not in str(result)
    assert (config.local / "delivery/scheduler-host.json").is_file()


def test_install_is_idempotent_but_other_host_cannot_take_over(tmp_path: Path) -> None:
    config, _, _ = explained_question(tmp_path)
    enable_delivery(config, "secret")
    launchctl = Launchctl(); binary = executable(tmp_path); env = {"VIDEO_EXTRACT_LARK_CHAT_ID": "secret"}
    first = install(config, home=tmp_path / "home", executable=binary, runner=launchctl,
                    environment=env, hostname="primary")
    again = install(config, home=tmp_path / "home", executable=binary, runner=launchctl,
                    environment=env, hostname="primary")
    conflict = install(config, home=tmp_path / "home", executable=binary, runner=launchctl,
                       environment=env, hostname="secondary")

    assert first["status"] == again["status"] == "completed"
    assert conflict["status"] == "awaiting_user"
    assert len([call for call in launchctl.calls if call[1] == "bootstrap"]) == 2


def test_missing_local_marker_is_paused_after_restore(tmp_path: Path) -> None:
    config, _, _ = explained_question(tmp_path)
    result = status(config, home=tmp_path / "restored-home", runner=Launchctl(), hostname="restored")
    assert result["status"] == "completed"
    assert result["result"]["enabled"] is False
    assert result["result"]["installed"] is False
    assert result["validation"]["unique_host"] == "not_verified"


def test_install_requires_an_enabled_matching_delivery_target(tmp_path: Path) -> None:
    config, _, _ = explained_question(tmp_path)
    launchctl = Launchctl(); binary = executable(tmp_path)
    missing = install(config, home=tmp_path / "home", executable=binary, runner=launchctl,
                      environment={"VIDEO_EXTRACT_LARK_CHAT_ID": "target"}, hostname="primary")
    enable_delivery(config, "different-target")
    changed = install(config, home=tmp_path / "home", executable=binary, runner=launchctl,
                      environment={"VIDEO_EXTRACT_LARK_CHAT_ID": "target"}, hostname="primary")

    assert missing["status"] == changed["status"] == "awaiting_user"
    assert launchctl.calls == []


def test_remove_unloads_only_workspace_label_and_leaves_disabled_marker(tmp_path: Path) -> None:
    config, _, _ = explained_question(tmp_path)
    launchctl = Launchctl(); binary = executable(tmp_path); home = tmp_path / "home"
    enable_delivery(config, "secret")
    installed = install(config, home=home, executable=binary, runner=launchctl,
                        environment={"VIDEO_EXTRACT_LARK_CHAT_ID": "secret"}, hostname="primary")
    removed = remove(config, home=home, runner=launchctl, hostname="primary")

    assert installed["status"] == removed["status"] == "completed"
    assert not list((home / "Library/LaunchAgents").glob("*.plist"))
    assert launchctl.calls[-1] == ["launchctl", "bootout", f"gui/{__import__('os').getuid()}/{installed['result']['label']}"]
    checked = status(config, home=home, runner=launchctl, hostname="primary")
    assert checked["result"]["enabled"] is False
