import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


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
    subprocess.run(["git", "-C", repo, "add", "main.py"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "initial"], check=True)
    commit = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()
    (repo / "main.py").write_text("answer = 2\n", encoding="utf-8")

    code, result = cli("source", "register", repo, "--workspace", config, "--json")

    assert code == 0
    assert result["result"]["kind"] == "code"
    basis = result["result"]["version_basis"]
    assert basis["git_commit"] == commit
    assert basis["working_tree_dirty"] is True
    assert basis["content_sha256"]
    assert result["result"]["storage"] == "reference"


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

    _, observed = cli("source", "verify", first["result"]["source_id"],
                      "--workspace", config, "--json")
    assert observed["result"]["content"] in {"one\n", "two\n"}
    assert observed["validation"]["snapshot"] == "passed"
    assert observed["validation"]["objects"] == "passed"


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
