import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from unittest.mock import patch

from video_extract.source_registry import register
from video_extract.workspace import WorkspaceConfig


def write_workspace(root: Path, *, results: str = "results", sources: str = "sources") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    config = root / "workspace.toml"
    config.write_text(
        f'''schema_version = 2
workspace_id = "11111111-1111-4111-8111-111111111111"
[paths]
project = "project"
results = "{results}"
sources = "{sources}"
derived = "derived"
local = "local"
''',
        encoding="utf-8",
    )
    return config


def cli(*args: object, env: dict[str, str] | None = None) -> tuple[int, dict]:
    completed = subprocess.run(
        [sys.executable, "-m", "video_extract.cli", *map(str, args)],
        cwd=Path(__file__).parents[1],
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
    )
    assert completed.stdout, completed.stderr
    return completed.returncode, json.loads(completed.stdout)


def run_recovery_command(command: str) -> subprocess.CompletedProcess[str]:
    """Run generated argv through this test environment, never a globally installed CLI."""
    argv = shlex.split(command)
    assert argv.pop(0) == "video-extract"
    return subprocess.run([sys.executable, "-m", "video_extract.cli", *argv],
                          cwd=Path(__file__).parents[1], capture_output=True, text=True)


def test_workspace_v2_show_uses_distinct_portable_roles(tmp_path: Path) -> None:
    config = write_workspace(tmp_path)

    code, result = cli("workspace", "show", "--workspace", config, "--json")

    assert code == 0
    assert result["api_version"] == 1
    assert result["workspace_id"] == "workspace-" + __import__("hashlib").sha256(
        b"explicit:11111111-1111-4111-8111-111111111111"
    ).hexdigest()[:16]
    assert result["status"] == "completed"
    assert result["result"]["schema_version"] == 2
    assert set(result["result"]["roles"]) == {"project", "results", "sources", "derived", "local"}


def test_workspace_v2_allows_declared_external_roots_but_rejects_symlink_escape(tmp_path: Path) -> None:
    external = tmp_path / "external-results"
    config = write_workspace(tmp_path / "workspace", results=str(external))
    code, shown = cli("workspace", "show", "--workspace", config, "--json")
    assert code == 0
    assert shown["result"]["roles"]["results"] == str(external)

    external.mkdir()
    (external / "objects").symlink_to(tmp_path / "outside", target_is_directory=True)
    (tmp_path / "outside").mkdir()
    source = tmp_path / "fixture.md"
    source.write_text("# Safe\n", encoding="utf-8")
    code, result = cli("source", "register", source, "--workspace", config, "--json")
    assert code == 1
    assert result["status"] == "failed"
    assert "symbolic link" in result["diagnostics"][0]


def test_register_document_reuses_source_identity_and_versions_content(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = tmp_path / "fixture.md"
    source.write_text("# Version one\n", encoding="utf-8")

    code, first = cli("source", "register", source, "--workspace", config, "--json")
    assert code == 0
    source_id = first["result"]["source_id"]
    first_version = first["result"]["source_version"]
    assert source_id.startswith("source-")
    assert first["result"]["package_schema_version"] == 6
    assert first["result"]["snapshot_schema_version"] == 1

    source.write_text("# Version two\n", encoding="utf-8")
    code, second = cli("source", "register", source, "--workspace", config, "--json")
    assert code == 0
    assert second["result"]["source_id"] == source_id
    assert second["result"]["source_version"] != first_version
    assert second["result"]["revision"] == first["result"]["revision"] + 1

    code, old = cli("source", "verify", source_id, "--source-version", first_version,
                    "--workspace", config, "--json")
    assert code == 0
    assert old["result"]["availability"] == "available_from_results"
    assert old["result"]["content"] == "# Version one\n"

    source.write_text("# Version one\n", encoding="utf-8")
    code, restored = cli("source", "register", source, "--workspace", config, "--json")
    assert code == 0
    assert restored["result"]["source_version"] == first_version
    assert restored["result"]["action"] == "restored_version"
    _, current = cli("source", "verify", source_id, "--workspace", config, "--json")
    assert current["result"]["source_version"] == first_version


def test_register_supplemental_source_preserves_public_provenance_and_unknown_is_explicit(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = tmp_path / "reference.md"
    source.write_text("# Public explanation\nOnly applies to CPython 3.13.\n", encoding="utf-8")
    provenance = tmp_path / "provenance.json"
    provenance.write_text(json.dumps({
        "origin": "https://example.org/reference",
        "author_or_organization": "Example Institute",
        "published_version_or_date": "2026-08-01",
        "accessed_at": "2026-09-17T09:00:00+08:00",
        "summary": "Explains the public API behavior.",
        "locator": "Section 2, API boundary",
        "applicability": "CPython 3.13 only",
        "verification": "manual_review_required",
    }), encoding="utf-8")

    code, registered = cli("source", "register", source, "--provenance", provenance,
                           "--workspace", config, "--json")
    assert code == 0
    code, verified = cli("source", "verify", registered["result"]["source_id"],
                         "--workspace", config, "--json")
    assert code == 0
    assert verified["result"]["provenance_status"] == "recorded"
    assert verified["result"]["provenance"] == json.loads(provenance.read_text())

    _, module = cli("learning", "module", "create", "--goal", "check applicability", "--scope", "API",
                    "--source-id", registered["result"]["source_id"],
                    "--source-version", registered["result"]["source_version"],
                    "--source-role", "supplemental_source", "--workspace", config, "--json")
    _, rooted = cli("learning", "thread", "create", "--module-id", module["result"]["module"]["module_id"],
                    "--root-question", "When does it apply?", "--workspace", config, "--json")
    _, prepared = cli("explanation", "prepare", "--question-id", rooted["result"]["question"]["question_id"],
                      "--profile", "linear_transform", "--workspace", config, "--json")
    assert prepared["result"]["source_context"][0]["provenance"]["applicability"] == "CPython 3.13 only"
    assert prepared["result"]["source_context"][0]["provenance"]["verification"] == "manual_review_required"

    plain = tmp_path / "plain.md"; plain.write_text("legacy-style source\n", encoding="utf-8")
    _, unannotated = cli("source", "register", plain, "--workspace", config, "--json")
    _, checked = cli("source", "verify", unannotated["result"]["source_id"],
                     "--workspace", config, "--json")
    assert checked["result"]["provenance_status"] == "unknown"
    assert checked["result"]["provenance"] is None


def test_import_alias_and_doctor_validate_the_published_store(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = tmp_path / "fixture.txt"
    source.write_text("portable\n", encoding="utf-8")
    code, imported = cli("source", "import", source, "--workspace", config, "--json")
    assert code == 0
    assert imported["result"]["package_schema_version"] == 6

    code, checked = cli("workspace", "doctor", "--workspace", config, "--json")
    assert code == 0
    assert checked["status"] == "completed"
    assert checked["result"]["snapshot"]["commit_id"] == imported["result"]["commit_id"]
    assert checked["validation"]["objects"] == "passed"


def test_relative_workspace_roots_move_without_changing_identity(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    config = write_workspace(first_root)
    source = tmp_path / "fixture.txt"
    source.write_text("portable\n", encoding="utf-8")
    _, registered = cli("source", "register", source, "--workspace", config, "--json")

    second_root = tmp_path / "second"
    first_root.rename(second_root)
    moved_config = second_root / "workspace.toml"
    code, verified = cli("source", "verify", registered["result"]["source_id"],
                         "--workspace", moved_config, "--json")

    assert code == 0
    assert verified["workspace_id"] == registered["workspace_id"]
    assert verified["operation_id"]
    assert verified["result"]["commit_id"] == registered["result"]["commit_id"]
    assert verified["result"]["content"] == "portable\n"


def test_register_code_directory_records_git_and_dirty_content(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    repo = tmp_path / "code"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "fixture@example.com"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "Fixture"], check=True)
    (repo / "main.py").write_text("answer = 1\n", encoding="utf-8")
    (repo / "removed.py").write_text("remove = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", repo, "add", "main.py", "removed.py"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "initial"], check=True)
    commit = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()
    (repo / "main.py").write_text("answer = 2\n", encoding="utf-8")
    (repo / "added.py").write_text("added = True\n", encoding="utf-8")
    (repo / "removed.py").unlink()

    code, result = cli("source", "register", repo, "--workspace", config, "--json")

    assert code == 0
    assert result["result"]["kind"] == "code"
    basis = result["result"]["version_basis"]
    assert basis["git_commit"] == commit
    assert basis["working_tree_dirty"] is True
    assert basis["content_sha256"]
    assert basis["dirty_files"] == {"modified": ["main.py"], "added": ["added.py"], "deleted": ["removed.py"]}
    assert result["result"]["storage"] == "reference"
    _, verified = cli("source", "verify", result["result"]["source_id"],
                      "--workspace", config, "--json")
    assert verified["result"]["observed_version_basis"] == basis


def test_git_provenance_is_scoped_to_registered_monorepo_directory_and_expands_rename(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    repo = tmp_path / "monorepo"; scope = repo / "packages" / "learn"; outside = repo / "packages" / "other"
    scope.mkdir(parents=True); outside.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "fixture@example.com"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "Fixture"], check=True)
    (scope / "old.py").write_text("value = 1\n"); (outside / "outside.py").write_text("outside = 1\n")
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "initial"], check=True)

    (outside / "outside.py").write_text("outside = 2\n")
    clean_scope = register(WorkspaceConfig.load(config), scope)
    assert clean_scope["result"]["version_basis"]["working_tree_dirty"] is False
    assert clean_scope["result"]["version_basis"]["dirty_files"] == {
        "modified": [], "added": [], "deleted": []}

    subprocess.run(["git", "-C", repo, "mv", "packages/learn/old.py", "packages/learn/new.py"], check=True)
    renamed = register(WorkspaceConfig.load(config), scope)
    assert renamed["result"]["version_basis"]["dirty_files"] == {
        "modified": [], "added": ["new.py"], "deleted": ["old.py"]}
    assert all(".." not in path for paths in renamed["result"]["version_basis"]["dirty_files"].values()
               for path in paths)


def test_git_provenance_cross_scope_renames_record_only_the_in_scope_side(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    repo = tmp_path / "monorepo"; scope = repo / "scope"; outside = repo / "outside"
    scope.mkdir(parents=True); outside.mkdir()
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "fixture@example.com"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "Fixture"], check=True)
    (scope / "moves-out.py").write_text("out = 1\n")
    (outside / "moves-in.py").write_text("inside = 1\n")
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "initial"], check=True)

    subprocess.run(["git", "-C", repo, "mv", "scope/moves-out.py", "outside/moves-out.py"], check=True)
    subprocess.run(["git", "-C", repo, "mv", "outside/moves-in.py", "scope/moves-in.py"], check=True)
    registered = register(WorkspaceConfig.load(config), scope)

    assert registered["result"]["version_basis"]["dirty_files"] == {
        "modified": [], "added": ["moves-in.py"], "deleted": ["moves-out.py"]}


def test_git_provenance_uses_literal_pathspec_for_special_registered_directory(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    repo = tmp_path / "monorepo"; scope = repo / "pkg[*]"; sibling = repo / "pkga"
    scope.mkdir(parents=True); sibling.mkdir()
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "fixture@example.com"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "Fixture"], check=True)
    (scope / "inside.py").write_text("inside = 1\n"); (sibling / "outside.py").write_text("outside = 1\n")
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "initial"], check=True)
    (scope / "inside.py").write_text("inside = 2\n")
    (sibling / "outside.py").write_text("outside = 2\n")

    registered = register(WorkspaceConfig.load(config), scope)

    assert registered["result"]["version_basis"]["dirty_files"] == {
        "modified": ["inside.py"], "added": [], "deleted": []}


def test_git_copy_status_records_only_destination_as_added(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    repo = tmp_path / "repo"; scope = repo / "scope"; scope.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "fixture@example.com"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "Fixture"], check=True)
    subprocess.run(["git", "-C", repo, "config", "status.renames", "copies"], check=True)
    original = "\n".join(f"shared_{index} = 'copy me'" for index in range(20)) + "\n"
    (scope / "source.py").write_text(original)
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "initial"], check=True)
    (scope / "copied.py").write_text(original)
    (scope / "source.py").write_text(original + "source_changed = True\n")
    subprocess.run(["git", "-C", repo, "add", "scope/copied.py", "scope/source.py"], check=True)

    raw = subprocess.check_output(
        ["git", "--literal-pathspecs", "-C", repo, "status", "--porcelain=v1", "-z", "--", "scope"])
    assert b"C  scope/copied.py\x00scope/source.py\x00" in raw, raw
    registered = register(WorkspaceConfig.load(config), scope)

    assert registered["result"]["version_basis"]["dirty_files"] == {
        "modified": ["source.py"], "added": ["copied.py"], "deleted": []}


def test_relocate_finds_same_version_and_does_not_accept_different_content(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = tmp_path / "one.md"
    source.write_text("same bytes\n", encoding="utf-8")
    _, registered = cli("source", "register", source, "--workspace", config, "--json")
    source_id = registered["result"]["source_id"]
    moved = tmp_path / "moved.md"
    source.rename(moved)

    code, relocated = cli("source", "relocate", source_id, moved, "--expected-revision",
                          registered["result"]["revision"], "--workspace", config, "--json")
    assert code == 0
    assert relocated["result"]["location"] == str(moved.resolve())

    changed = tmp_path / "changed.md"
    changed.write_text("different bytes\n", encoding="utf-8")
    code, rejected = cli("source", "relocate", source_id, changed, "--expected-revision",
                         relocated["result"]["revision"], "--workspace", config, "--json")
    assert code == 1
    assert rejected["status"] == "recoverable_failure"
    assert rejected["result"]["availability"] == "version_mismatch"


def test_expected_revision_conflict_preserves_candidate(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = tmp_path / "fixture.txt"
    source.write_text("one\n", encoding="utf-8")
    _, first = cli("source", "register", source, "--workspace", config, "--json")
    source.write_text("two\n", encoding="utf-8")

    code, conflict = cli("source", "register", source, "--expected-revision", 0,
                         "--workspace", config, "--json")

    assert code == 3
    assert conflict["status"] == "awaiting_user"
    candidate = Path(conflict["result"]["candidate"])
    assert candidate.is_file()
    proposed = json.loads(candidate.read_text(encoding="utf-8"))["proposed_version"]
    assert proposed["content_sha256"]
    assert proposed["object_sha256"] == proposed["content_sha256"]
    _, current = cli("source", "verify", first["result"]["source_id"],
                     "--workspace", config, "--json")
    assert current["result"]["source_version"] == first["result"]["source_version"]


@pytest.mark.parametrize("fault", ["before_publish", "after_publish"])
def test_publish_fault_exposes_only_a_complete_snapshot(tmp_path: Path, fault: str) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = tmp_path / "fixture.txt"
    source.write_text("one\n", encoding="utf-8")
    _, first = cli("source", "register", source, "--workspace", config, "--json")
    source.write_text("two\n", encoding="utf-8")

    code, failed = cli("source", "register", source, "--expected-revision",
                       first["result"]["revision"], "--workspace", config, "--json",
                       env={"VIDEO_EXTRACT_TEST_FAULT": fault})
    assert code == 1
    assert failed["status"] == "recoverable_failure"
    assert failed["operation_id"]
    if fault == "after_publish":
        assert failed["result"]["source_id"] == first["result"]["source_id"]
        assert failed["result"]["source_version"] != first["result"]["source_version"]
        assert failed["result"]["revision"] == first["result"]["revision"] + 1
        assert failed["result"]["commit_id"].startswith("commit-")
        assert first["result"]["source_id"] in failed["next_action"]["command"]
        assert "source reconcile" in failed["next_action"]["command"]

    _, observed = cli("source", "verify", first["result"]["source_id"],
                      "--workspace", config, "--json")
    assert observed["result"]["content"] in {"one\n", "two\n"}
    assert observed["validation"]["snapshot"] == "passed"
    assert observed["validation"]["objects"] == "passed"


def test_doctor_and_verify_reject_nested_schema_corruption(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = tmp_path / "fixture.txt"
    source.write_text("one\n", encoding="utf-8")
    _, first = cli("source", "register", source, "--workspace", config, "--json")
    results = config.parent / "results"
    pointer_path = results / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    manifest_path = results / "commits" / f'{pointer["commit_id"]}.json'
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = manifest["sources"][first["result"]["source_id"]]["versions"][first["result"]["source_version"]]
    version["entries"] = [{"path": "bad", "sha256": 7, "size": "not-an-integer"}]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    pointer["manifest_sha256"] = __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest()
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

    code, checked = cli("workspace", "doctor", "--workspace", config, "--json")
    assert code == 1
    assert checked["status"] == "recoverable_failure"
    assert "snapshot-v1.schema.json" in checked["diagnostics"][0]
    code, verified = cli("source", "verify", first["result"]["source_id"],
                         "--workspace", config, "--json")
    assert code == 1
    assert verified["status"] == "failed"
    assert "snapshot-v1.schema.json" in verified["diagnostics"][0]


def test_doctor_rejects_version_object_missing_from_snapshot_objects(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = tmp_path / "fixture.txt"
    source.write_text("one\n", encoding="utf-8")
    _, first = cli("source", "register", source, "--workspace", config, "--json")
    results = config.parent / "results"
    pointer_path = results / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    manifest_path = results / "commits" / f'{pointer["commit_id"]}.json'
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["objects"][first["result"]["source_version"].removeprefix("source-version-")]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    pointer["manifest_sha256"] = __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest()
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

    code, checked = cli("workspace", "doctor", "--workspace", config, "--json")

    assert code == 1
    assert checked["status"] == "recoverable_failure"
    assert "not reachable through snapshot.objects" in checked["diagnostics"][0]


def test_publish_syncs_object_manifest_and_pointer_directories_in_order(tmp_path: Path) -> None:
    config_path = write_workspace(tmp_path / "workspace")
    source = tmp_path / "fixture.txt"
    source.write_text("durable\n", encoding="utf-8")
    synced: list[str] = []

    with patch("video_extract.source_registry._sync_directory",
               side_effect=lambda path: synced.append(Path(path).name)):
        result = register(WorkspaceConfig.load(config_path), source)

    assert result["status"] == "completed"
    assert synced.index("objects") < synced.index("commits")
    assert synced.index("commits") < len(synced) - 1
    assert synced[-1] == "results"


def test_directory_sync_failure_is_not_reported_as_success(tmp_path: Path) -> None:
    config_path = write_workspace(tmp_path / "workspace")
    source = tmp_path / "fixture.txt"
    source.write_text("durable\n", encoding="utf-8")

    with patch("video_extract.source_registry._sync_directory", side_effect=OSError("sync failed")):
        result = register(WorkspaceConfig.load(config_path), source)

    assert result["status"] == "recoverable_failure"
    assert "sync failed" in result["diagnostics"][0]


def test_pointer_directory_sync_failure_is_visibility_not_durability(tmp_path: Path) -> None:
    config_path = write_workspace(tmp_path / "workspace")
    workspace = WorkspaceConfig.load(config_path)
    source = tmp_path / "fixture.txt"
    source.write_text("one\n", encoding="utf-8")
    first = register(workspace, source)
    source.write_text("two\n", encoding="utf-8")
    real_sync = __import__("video_extract.source_registry", fromlist=["_sync_directory"])._sync_directory
    results_syncs = 0

    def fail_after_pointer(path: Path) -> None:
        nonlocal results_syncs
        if Path(path) == workspace.results:
            results_syncs += 1
            if results_syncs == 2:
                raise OSError("pointer directory sync failed")
        real_sync(path)

    with patch("video_extract.source_registry._sync_directory", side_effect=fail_after_pointer):
        result = register(workspace, source, expected_revision=first["result"]["revision"])

    assert result["status"] == "recoverable_failure"
    assert result["result"]["logical_visibility"] == "current"
    assert result["result"]["durability"] == "unknown"
    assert result["validation"]["snapshot"] == "uncertain"
    assert result["validation"]["durability"] == "unknown"
    assert result["result"].get("published") is not True
    command = result["next_action"]["command"]
    assert "source reconcile" in command
    assert first["result"]["source_id"] in command

    completed = run_recovery_command(command)
    reconciled = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert reconciled["status"] == "completed"
    assert reconciled["result"]["durability"] == "confirmed"


def test_retry_resyncs_existing_object_directories_after_first_barrier_failure(tmp_path: Path) -> None:
    config_path = write_workspace(tmp_path / "workspace")
    workspace = WorkspaceConfig.load(config_path)
    source = tmp_path / "fixture.txt"
    source.write_text("one\n", encoding="utf-8")
    first = register(workspace, source)
    source.write_text("two\n", encoding="utf-8")
    digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
    failed_dir = workspace.results / "objects" / digest[:2]
    real_sync = __import__("video_extract.source_registry", fromlist=["_sync_directory"])._sync_directory
    failed_once = False

    def fail_object_once(path: Path) -> None:
        nonlocal failed_once
        if Path(path) == failed_dir and not failed_once:
            failed_once = True
            raise OSError("object hash directory sync failed")
        real_sync(path)

    with patch("video_extract.source_registry._sync_directory", side_effect=fail_object_once):
        failed = register(workspace, source, expected_revision=first["result"]["revision"])
    assert failed["status"] == "recoverable_failure"
    assert failed["next_action"]["type"] == "retry"

    synced: list[Path] = []
    with patch("video_extract.source_registry._sync_directory",
               side_effect=lambda path: (synced.append(Path(path)), real_sync(path))[1]):
        retried = register(workspace, source, expected_revision=first["result"]["revision"])

    assert retried["status"] == "completed"
    assert failed_dir in synced
    assert workspace.results / "objects" in synced


def test_retry_resyncs_existing_commit_directories_after_first_barrier_failure(tmp_path: Path) -> None:
    config_path = write_workspace(tmp_path / "workspace")
    workspace = WorkspaceConfig.load(config_path)
    source = tmp_path / "fixture.txt"
    source.write_text("one\n", encoding="utf-8")
    first = register(workspace, source)
    source.write_text("two\n", encoding="utf-8")
    commits = workspace.results / "commits"
    real_sync = __import__("video_extract.source_registry", fromlist=["_sync_directory"])._sync_directory
    failed_once = False

    def fail_commit_once(path: Path) -> None:
        nonlocal failed_once
        if Path(path) == commits and not failed_once:
            failed_once = True
            raise OSError("commit directory sync failed")
        real_sync(path)

    fixed_time = "2026-09-17T00:00:00+00:00"
    with patch("video_extract.source_registry._now", return_value=fixed_time), \
            patch("video_extract.source_registry._sync_directory", side_effect=fail_commit_once):
        failed = register(workspace, source, expected_revision=first["result"]["revision"])
    assert failed["status"] == "recoverable_failure"

    synced: list[Path] = []
    with patch("video_extract.source_registry._now", return_value=fixed_time), \
            patch("video_extract.source_registry._sync_directory",
                  side_effect=lambda path: (synced.append(Path(path)), real_sync(path))[1]):
        retried = register(workspace, source, expected_revision=first["result"]["revision"])

    assert retried["status"] == "completed"
    assert commits in synced
    assert workspace.results in synced


def test_pre_publish_retry_replays_source_id_title_and_operation_identity(tmp_path: Path) -> None:
    config_path = write_workspace(tmp_path / "workspace")
    workspace = WorkspaceConfig.load(config_path)
    source = tmp_path / "source with spaces.txt"
    source.write_text("one\n", encoding="utf-8")
    title = "A title with spaces and 'single' plus \"double\" quotes"
    real_sync = __import__("video_extract.source_registry", fromlist=["_sync_directory"])._sync_directory
    failed_once = False

    def fail_first_hash_dir(path: Path) -> None:
        nonlocal failed_once
        if Path(path).parent.name == "objects" and not failed_once:
            failed_once = True
            raise OSError("first hash directory sync failed")
        real_sync(path)

    with patch("video_extract.source_registry._sync_directory", side_effect=fail_first_hash_dir):
        failed = register(workspace, source, title=title, expected_revision=0)

    assert failed["status"] == "recoverable_failure"
    command = failed["next_action"]["command"]
    assert "--source-id" in command
    assert "--title" in command
    completed = run_recovery_command(command)
    retried = json.loads(completed.stdout)

    assert completed.returncode == 0
    assert retried["status"] == "completed"
    assert retried["result"]["source_id"] == failed["result"]["source_id"]
    assert retried["result"]["title"] == title
    assert retried["operation_id"] == failed["operation_id"]
    _, verified = cli("source", "verify", retried["result"]["source_id"],
                      "--workspace", config_path, "--json")
    assert verified["result"]["title"] == title


def test_pre_publish_retry_replays_immutable_provenance(tmp_path: Path) -> None:
    config_path = write_workspace(tmp_path / "workspace")
    workspace = WorkspaceConfig.load(config_path)
    source = tmp_path / "source with spaces.txt"; source.write_text("one\n")
    provenance = {"origin": "https://example.org/a b", "author_or_organization": "Example Org",
                  "published_version_or_date": "2026-09-01", "accessed_at": "2026-09-17T09:00:00+08:00",
                  "summary": "public fact", "locator": "section 1", "applicability": "version 1",
                  "verification": "manual_review_required"}
    real_sync = __import__("video_extract.source_registry", fromlist=["_sync_directory"])._sync_directory
    failed_once = False

    def fail_first_hash_dir(path: Path) -> None:
        nonlocal failed_once
        if Path(path).parent.name == "objects" and not failed_once:
            failed_once = True
            raise OSError("first hash directory sync failed")
        real_sync(path)

    with patch("video_extract.source_registry._sync_directory", side_effect=fail_first_hash_dir):
        failed = register(workspace, source, expected_revision=0, provenance=provenance)
    assert failed["status"] == "recoverable_failure"
    assert "--provenance" in failed["next_action"]["command"]
    completed = run_recovery_command(failed["next_action"]["command"])
    assert completed.returncode == 0, completed.stderr + completed.stdout
    retried = json.loads(completed.stdout)
    _, verified = cli("source", "verify", retried["result"]["source_id"],
                      "--workspace", config_path, "--json")
    assert verified["result"]["provenance"] == provenance


def test_committed_source_id_cannot_be_recovered_to_unmapped_different_source(tmp_path: Path) -> None:
    config_path = write_workspace(tmp_path / "workspace")
    first = tmp_path / "first.txt"
    first.write_text("first source\n", encoding="utf-8")
    _, registered = cli("source", "register", first, "--workspace", config_path, "--json")
    location_map = config_path.parent / "local" / "source-locations.json"
    location_map.unlink()
    other = tmp_path / "other.txt"
    other.write_text("unrelated source B\n", encoding="utf-8")

    code, rejected = cli("source", "register", other, "--source-id",
                         registered["result"]["source_id"], "--expected-revision",
                         registered["result"]["revision"], "--workspace", config_path, "--json")

    assert code == 1
    assert rejected["status"] == "failed"
    assert "source relocate" in rejected["diagnostics"][0]
    assert "formal association" in rejected["diagnostics"][0]


def test_committed_source_update_retry_accepts_matching_trusted_mapping(tmp_path: Path) -> None:
    config_path = write_workspace(tmp_path / "workspace")
    workspace = WorkspaceConfig.load(config_path)
    source = tmp_path / "fixture.txt"
    source.write_text("one\n", encoding="utf-8")
    first = register(workspace, source, title="Stable title")
    source.write_text("two\n", encoding="utf-8")
    real_sync = __import__("video_extract.source_registry", fromlist=["_sync_directory"])._sync_directory
    failed_once = False

    def fail_first_hash_dir(path: Path) -> None:
        nonlocal failed_once
        if Path(path).parent.name == "objects" and not failed_once:
            failed_once = True
            raise OSError("first hash directory sync failed")
        real_sync(path)

    with patch("video_extract.source_registry._sync_directory", side_effect=fail_first_hash_dir):
        failed = register(workspace, source, title="Stable title",
                          expected_revision=first["result"]["revision"])
    completed = run_recovery_command(failed["next_action"]["command"])
    retried = json.loads(completed.stdout)

    assert completed.returncode == 0
    assert retried["result"]["source_id"] == first["result"]["source_id"]
    assert retried["result"]["title"] == "Stable title"
    assert retried["operation_id"] == failed["operation_id"]


def test_new_source_commands_reject_unconverted_workspace_v1(tmp_path: Path) -> None:
    config = tmp_path / "workspace.toml"
    config.write_text('''schema_version = 1
[paths]
project = "project"
media = "media"
obsidian = "vault"
[obsidian]
generated = "generated"
threads = "threads"
concepts = "concepts"
review = "REVIEW.md"
''', encoding="utf-8")
    source = tmp_path / "fixture.txt"
    source.write_text("legacy\n", encoding="utf-8")

    code, result = cli("source", "register", source, "--workspace", config, "--json")

    assert code == 1
    assert result["status"] == "unsupported"
    assert result["next_action"]["command"] == "video-extract migration plan"
