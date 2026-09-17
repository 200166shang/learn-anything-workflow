import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_extract import cli


RETIRED_COMMANDS = {
    "migrate-legacy": (
        ["--library-root", "/unused", "--mode", "plan", "--report", "/unused/report.json",
         "--journal", "/unused/journal.json", "--json"],
        "video-extract migration plan",
    ),
    "scan": (["source", "--platform", "youtube", "--output", "/unused/catalog.json", "--json"],
             "video-extract plan"),
    "acquire": (["source", "--platform", "youtube", "--output", "/unused/package", "--json"],
                "video-extract ensure"),
    "transcribe": (["--video", "/unused/video.mp4", "--output", "/unused/package", "--json"],
                   "video-extract notes prepare"),
    "prepare-evidence": (["--video", "/unused/video.mp4", "--output", "/unused/package", "--json"],
                         "video-extract notes prepare"),
}


def test_retired_commands_are_hidden_while_current_media_and_migration_commands_remain(
    capsys,
) -> None:
    root_help = cli.parser().format_help()
    for command in RETIRED_COMMANDS:
        assert command not in root_help
    for command in ("plan", "ensure", "notes", "capability", "migration"):
        assert command in root_help

    with pytest.raises(SystemExit) as raised:
        cli.parser().parse_args(["migration", "--help"])
    assert raised.value.code == 0
    migration_help = capsys.readouterr().out
    for action in ("plan", "convert", "verify", "cutover", "rollback"):
        assert action in migration_help


@pytest.mark.parametrize(("command", "arguments_and_replacement"), RETIRED_COMMANDS.items())
def test_retired_commands_return_structured_unsupported_without_running_scripts(
    command: str, arguments_and_replacement: tuple[list[str], str], capsys, monkeypatch
) -> None:
    arguments, replacement = arguments_and_replacement
    monkeypatch.setattr("video_extract.command_response.engineering_revision", lambda: "test-revision")

    parsed = cli.parser().parse_args([command, *arguments])
    code = parsed.func(parsed)
    result = json.loads(capsys.readouterr().out)

    assert code == 1
    assert result["api_version"] == 1
    assert result["status"] == "unsupported"
    assert result["next_action"] == {"command": replacement}
    assert "cannot execute legacy scripts" in result["diagnostics"][0]


def test_explicit_migration_actions_remain_public() -> None:
    parser = cli.parser()
    for action in ("plan", "convert", "verify", "cutover", "rollback"):
        arguments = ["migration", action, "--batch", "batch-1", "--workspace", "/workspace.toml"]
        if action == "plan":
            arguments.extend(["--legacy-package", "/legacy/package", "--legacy-thread", "/legacy/thread.json"])
        if action == "cutover":
            arguments.extend(["--authorization", "/authorization.json"])
        assert parser.parse_args(arguments).func is cli.cmd_migration


@pytest.mark.parametrize("action", ("plan", "apply", "check"))
def test_install_commands_pass_obsidian_plugins_root(
    action: str, tmp_path: Path, capsys, monkeypatch
) -> None:
    calls = []

    def completed(*arguments):
        calls.append(arguments)
        return {"status": "completed"}

    monkeypatch.setattr("video_extract.installation.plan", completed)
    monkeypatch.setattr("video_extract.installation.apply", completed)
    monkeypatch.setattr("video_extract.installation.inspect", completed)
    plugins = tmp_path / "obsidian" / "plugins"
    arguments = [
        "install", action, "--agents-root", str(tmp_path / "agents"),
        "--codex-root", str(tmp_path / "codex"), "--obsidian-plugins-root", str(plugins), "--json",
    ]

    parsed = cli.parser().parse_args(arguments)
    assert parsed.func(parsed) == 0
    capsys.readouterr()

    assert calls
    assert calls[0][-1] == plugins.resolve()


def test_recursive_discovery_prunes_migration_preservation_and_legacy_roots(
    tmp_path: Path, monkeypatch
) -> None:
    included = tmp_path / "media" / "current-package"
    excluded_roots = (
        tmp_path / "local" / "migration-batches" / "batch-1",
        tmp_path / "local" / "migration-preserved" / "batch-1",
        tmp_path / "media" / "migration" / "quarantine" / "unknown",
        tmp_path / "backups" / "snapshot",
        tmp_path / "legacy-read-only" / "course",
    )
    for package in (included, *excluded_roots):
        package.mkdir(parents=True)
        (package / "manifest.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        cli, "validate", lambda path: SimpleNamespace(to_dict=lambda: {"package": str(path)})
    )

    assert cli._discover(tmp_path) == [{"package": str(included)}]
