import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from video_extract.authoritative_notes import finalize_note, prepare_note
from video_extract.backup import create_backup, restore_backup, verify_backup
from video_extract.learning import create_module, create_thread
from video_extract.source_registry import register, verify
from video_extract.workspace import WorkspaceConfig
from video_extract.cli import main


@pytest.mark.parametrize("kind", ["file", "dangling_symlink"])
def test_backup_cli_rejects_invalid_receipt_root(tmp_path: Path, kind: str) -> None:
    config = workspace(tmp_path / "workspace")
    config.results.mkdir(parents=True)
    root = config.results / "operation-receipts"
    if kind == "file":
        root.write_text("corrupt receipt root", encoding="utf-8")
    else:
        root.symlink_to(tmp_path / "missing-receipts", target_is_directory=True)
    backup = tmp_path / "backup"
    completed = subprocess.run(
        [sys.executable, "-m", "video_extract.cli", "backup", "create", str(backup),
         "--workspace", str(config.config_path), "--json"], capture_output=True, text=True)
    result = json.loads(completed.stdout)
    assert completed.returncode != 0
    assert result["status"] != "completed"
    assert not backup.exists()


def test_restore_cli_rejects_receipt_changed_during_copy(tmp_path: Path, monkeypatch, capsys) -> None:
    config, _, _ = populated(tmp_path)
    backup = tmp_path / "backup"
    assert create_backup(config, backup)["status"] == "completed"
    target = workspace(tmp_path / "restored")
    copyfile = shutil.copyfile

    def concurrent_copy(source, destination, *args, **kwargs):
        if Path(source).name == "operation-example.json":
            receipt = json.loads(Path(source).read_text())
            receipt["receipt"]["state"] = "changed_after_verification"
            Path(source).write_text(json.dumps(receipt))
        return copyfile(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", concurrent_copy)
    monkeypatch.setattr(sys, "argv", ["video-extract", "backup", "restore", str(backup),
                                     "--workspace", str(target.config_path), "--json"])
    code = main()
    result = json.loads(capsys.readouterr().out)
    assert code != 0
    assert result["status"] == "recoverable_failure"
    assert not target.results.exists()


def workspace(root: Path, *, results: str = "results") -> WorkspaceConfig:
    root.mkdir(parents=True)
    config = root / "workspace.toml"
    config.write_text(f'''schema_version = 2
workspace_id = "11111111-1111-4111-8111-111111111111"
[paths]
project = "project"
results = "{results}"
sources = "sources"
derived = "derived"
local = "local"
''', encoding="utf-8")
    return WorkspaceConfig.load(config)


def populated(tmp_path: Path, *, include_learning: bool = True) -> tuple[WorkspaceConfig, dict, dict]:
    config = workspace(tmp_path / "source-workspace")
    source_path = tmp_path / "lesson.md"
    source_path.write_text("# Mechanism\n\nInput becomes output.\n", encoding="utf-8")
    source = register(config, source_path)["result"]
    prepared = prepare_note(config, source["source_id"], source["source_version"])
    request = tmp_path / "note.json"
    request.write_text(json.dumps({
        "schema_version": 1, "source_id": source["source_id"],
        "source_version": source["source_version"], "expected_revision": 0,
        "markdown": "# Note\n\nInput becomes output.\n",
        "citations": [{"claim": "Input becomes output", "locator": "paragraph:2",
                       "locator_type": "paragraph"}],
        "corrections": [], "attachments": [],
        "association": {"status": "not_applicable", "evidence": None},
    }), encoding="utf-8")
    finalize_note(config, request)
    thread = {}
    if include_learning:
        module = create_module(config, "Understand mechanism", "one lesson",
                               source["source_id"], source["source_version"])["result"]["module"]
        thread = create_thread(config, module["module_id"], "How does it work?")["result"]
    receipt = config.results / "operation-receipts" / "operation-example.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"schema_version": 1, "operation_id": "operation-example",
        "status": "completed", "authoritative_revision": 1, "authoritative_digest": "a" * 64,
        "intent": {"capability_id": "media.acquire"}, "artifact_facts": {}, "attempts": [],
        "reconciliation": [], "receipt": {"state": "verified"}}),
                       encoding="utf-8")
    return config, source, thread


@pytest.mark.parametrize("notes", [True, False])
def test_backup_cli_preserves_uninitialized_domains(tmp_path: Path, monkeypatch, capsys, notes: bool) -> None:
    config = populated(tmp_path, include_learning=False)[0] if notes else workspace(tmp_path / "empty")
    backup = tmp_path / "backup"
    target = workspace(tmp_path / "restored")
    for action, selected in (("create", config), ("verify", None), ("restore", target)):
        argv = ["video-extract", "backup", action, str(backup), "--json"]
        if selected:
            argv += ["--workspace", str(selected.config_path)]
        monkeypatch.setattr(sys, "argv", argv)
        code = main()
        result = json.loads(capsys.readouterr().out)
        assert code == 0, result
    manifest = json.loads((backup / "backup-manifest.json").read_text())
    empty = manifest["authorities"]["learning"]
    assert empty["commit_id"] is None and empty["revision"] == 0 and empty["schema_version"] is None
    assert not (target.results / "learning/current.json").exists()
    if notes:
        assert (target.results / "source-notes/current.json").is_file()
    else:
        assert manifest["entries"] == []


@pytest.mark.parametrize("damage", ["missing", "directory", "corrupt", "symlink"])
def test_backup_cli_rejects_damaged_published_authority(tmp_path: Path, monkeypatch, capsys, damage: str) -> None:
    config, _, _ = populated(tmp_path)
    pointer = config.results / "learning/current.json"
    original = pointer.read_bytes()
    pointer.unlink()
    if damage == "directory":
        pointer.mkdir()
    elif damage == "corrupt":
        pointer.write_text("{}")
    elif damage == "symlink":
        outside = tmp_path / "outside-pointer.json"
        outside.write_bytes(original)
        pointer.symlink_to(outside)
    backup = tmp_path / "backup"
    monkeypatch.setattr(sys, "argv", ["video-extract", "backup", "create", str(backup),
                                     "--workspace", str(config.config_path), "--json"])
    code = main()
    result = json.loads(capsys.readouterr().out)
    assert code != 0, result
    assert not backup.exists()


def test_create_pins_and_deeply_verifies_complete_results_generation(tmp_path: Path) -> None:
    config, source, thread = populated(tmp_path)
    backup = tmp_path / "backup"

    created = create_backup(config, backup)
    checked = verify_backup(backup)

    assert created["status"] == checked["status"] == "completed"
    manifest = json.loads((backup / "backup-manifest.json").read_text())
    assert manifest["commit_id"].startswith("backup-commit-")
    assert set(manifest["authorities"]) == {"sources", "notes", "learning", "review", "practice", "cards", "operation_receipts"}
    assert all(authority["root_sha256"] for authority in manifest["authorities"].values())
    assert manifest["authorities"]["sources"]["commit_id"].startswith("commit-")
    assert manifest["authorities"]["notes"]["commit_id"].startswith("note-commit-")
    assert manifest["authorities"]["learning"]["commit_id"].startswith("learning-commit-")
    paths = {entry["path"] for entry in manifest["entries"]}
    assert "payload/operation-receipts/operation-example.json" in paths
    assert not any("local" in path or "candidate" in path or ".tmp" in path for path in paths)
    assert source["source_id"] in (backup / "payload/commits" /
        f'{manifest["authorities"]["sources"]["commit_id"]}.json').read_text()
    assert thread["thread"]["thread_id"] in (backup / "payload/learning/commits" /
        f'{manifest["authorities"]["learning"]["commit_id"]}.json').read_text()


def test_verify_detects_missing_tampered_and_broken_linked_objects(tmp_path: Path) -> None:
    config, _, _ = populated(tmp_path)
    backup = tmp_path / "backup"
    create_backup(config, backup)
    manifest = json.loads((backup / "backup-manifest.json").read_text())
    object_entry = next(item for item in manifest["entries"] if "/objects/" in item["path"])
    (backup / object_entry["path"]).write_bytes(b"tampered")

    checked = verify_backup(backup)

    assert checked["status"] == "failed"
    assert checked["validation"]["digests"] == "failed"


def test_restore_requires_empty_explicit_results_and_pauses_external_actions(tmp_path: Path) -> None:
    config, source, thread = populated(tmp_path)
    backup = tmp_path / "backup"
    create_backup(config, backup)
    target = workspace(tmp_path / "restored-workspace", results="restored-results")

    restored = restore_backup(backup, target)

    assert restored["status"] == "completed"
    assert verify(target, source["source_id"], source["source_version"])["status"] == "completed"
    assert (target.results / "source-notes/current.json").is_file()
    assert (target.results / "learning/current.json").is_file()
    state = json.loads((target.results / "recovery-state.json").read_text())
    assert state["delivery"] == "paused"
    assert state["external_operations"] == "paused"
    assert state["unique_host_verified"] is False
    assert thread["thread"]["thread_id"] in (target.results / "learning/commits" /
        f'{json.loads((target.results / "learning/current.json").read_text())["commit_id"]}.json').read_text()

    second = restore_backup(backup, target)
    assert second["status"] == "awaiting_user"
    assert second["validation"]["target"] == "conflict"


def test_restore_rejects_corruption_without_publishing_partial_results(tmp_path: Path) -> None:
    config, _, _ = populated(tmp_path)
    backup = tmp_path / "backup"
    create_backup(config, backup)
    manifest = json.loads((backup / "backup-manifest.json").read_text())
    victim = next(item for item in manifest["entries"] if item["path"].endswith("current.json"))
    (backup / victim["path"]).unlink()
    target = workspace(tmp_path / "restored-workspace", results="restored-results")

    restored = restore_backup(backup, target)

    assert restored["status"] == "failed"
    assert not target.results.exists()


def test_create_interruption_never_publishes_a_completed_backup(tmp_path: Path, monkeypatch) -> None:
    config, _, _ = populated(tmp_path)
    backup = tmp_path / "backup"
    monkeypatch.setenv("VIDEO_EXTRACT_BACKUP_TEST_FAULT", "before_publish")

    created = create_backup(config, backup)

    assert created["status"] == "recoverable_failure"
    assert not backup.exists()


def test_public_cli_exposes_create_verify_and_restore(tmp_path: Path) -> None:
    config, _, _ = populated(tmp_path)
    backup = tmp_path / "cli-backup"
    def run(*args: object) -> tuple[int, dict]:
        completed = subprocess.run([sys.executable, "-m", "video_extract.cli", *map(str, args), "--json"],
                                   capture_output=True, text=True)
        return completed.returncode, json.loads(completed.stdout)

    create_code, created = run("backup", "create", backup, "--workspace", config.config_path)
    verify_code, checked = run("backup", "verify", backup)
    target = workspace(tmp_path / "cli-restored", results="restored-results")
    restore_code, restored = run("backup", "restore", backup, "--workspace", target.config_path)

    assert (create_code, verify_code, restore_code) == (0, 0, 0)
    assert created["status"] == checked["status"] == restored["status"] == "completed"


def test_result_only_restore_marks_an_absent_referenced_source_without_losing_learning(tmp_path: Path) -> None:
    config = workspace(tmp_path / "source-workspace")
    source_tree = tmp_path / "code-source"; source_tree.mkdir()
    (source_tree / "main.py").write_text("print('evidence')\n", encoding="utf-8")
    source = register(config, source_tree)["result"]
    module = create_module(config, "Understand code", "main.py", source["source_id"],
                           source["source_version"])["result"]["module"]
    create_thread(config, module["module_id"], "Why is this output produced?")
    backup = tmp_path / "backup"
    completed = subprocess.run([sys.executable, "-m", "video_extract.cli", "backup", "create", str(backup),
                               "--workspace", str(config.config_path), "--json"], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout
    target = workspace(tmp_path / "restored", results="restored-results")
    source_tree.rename(tmp_path / "code-source-away")

    restored = restore_backup(backup, target)

    assert restored["status"] == "completed"
    assert restored["result"]["source_availability"] == [{
        "source_id": source["source_id"], "source_version": source["source_version"],
        "availability": "missing_external_source"}]
    pointer = json.loads((target.results / "learning/current.json").read_text())
    learning = json.loads((target.results / "learning/commits" / f'{pointer["commit_id"]}.json').read_text())
    assert module["module_id"] in learning["record"]["modules"]
