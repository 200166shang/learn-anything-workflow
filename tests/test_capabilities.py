import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch

import video_extract.capabilities as capability_module
from video_extract.capabilities import CAPABILITIES, Capability
from video_extract.cli import main, parser
from video_extract.source_import import import_source
from video_extract.workspace import WorkspaceConfig


def make_workspace(root: Path) -> WorkspaceConfig:
    (root / "project").mkdir()
    (root / "media").mkdir()
    (root / "vault").mkdir()
    config = root / "workspace.toml"
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
    return WorkspaceConfig.load(config)


def run_cli(*arguments: str) -> tuple[int, dict]:
    stdout = io.StringIO()
    with patch("sys.argv", ["video-extract", *arguments, "--json"]), contextlib.redirect_stdout(stdout):
        code = main()
    return code, json.loads(stdout.getvalue())


def test_help_exposes_stable_capability_entry() -> None:
    assert "capability" in parser().format_help()


def test_list_declares_only_real_capability_contracts() -> None:
    code, result = run_cli("capability", "list")

    assert code == 0
    assert result["api_version"] == 1
    assert result["status"] == "completed"
    assert [item["id"] for item in result["result"]["capabilities"]] == ["source.notes"]
    declared = result["result"]["capabilities"][0]
    assert declared["contract_version"] == 1
    assert declared["input_type"] == "source-notes-request-v1"
    assert declared["output_type"] == "command-response-v1"
    assert declared["side_effect"] == "workspace_write"
    assert declared["authorization_category"] == "local_workspace"
    assert declared["recovery_query"] == "capability run source.notes with the same request"
    assert declared["implementation"] == "video_extract.capabilities:run_source_notes"


def test_check_reports_missing_entry_at_the_single_maintenance_mapping() -> None:
    missing = Capability(
        **{**CAPABILITIES["source.notes"].__dict__, "implementation": "video_extract.missing:run"}
    )
    with patch.dict(CAPABILITIES, {"source.notes": missing}, clear=True):
        code, result = run_cli("capability", "check", "source.notes")

    assert code == 3
    assert result["status"] == "missing_dependency"
    assert result["result"]["capabilities"][0]["implementation_state"] == "missing"
    assert result["diagnostics"] == [
        "source.notes implementation is unavailable; update video_extract.capabilities:CAPABILITIES"
    ]


def test_run_existing_capability_normalizes_model_pause_and_exit_code(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    source = tmp_path / "input.md"
    source.write_text("# Fixture\n\nInput becomes output.\n", encoding="utf-8")
    package = Path(import_source(source, workspace)["package"])
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "contract_version": 1,
        "action": "prepare",
        "workspace": str(workspace.config_path),
        "package": str(package),
        "source_version": "fixture-v1",
    }), encoding="utf-8")

    code, result = run_cli("capability", "run", "source.notes", "--request", str(request))

    assert code == 3
    assert result["api_version"] == 1
    assert result["workspace_id"]
    assert result["operation_id"]
    assert result["status"] == "awaiting_model"
    assert result["observed_revision"]
    assert result["validation"]["contract_version"] == "passed"
    assert result["provenance"]["capability_id"] == "source.notes"
    assert result["provenance"]["source_version"] == "fixture-v1"
    assert result["next_action"]["type"] == "model"
    assert result["diagnostics"] == []


def test_compatible_replacement_only_changes_the_single_mapping(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    source = tmp_path / "replacement.md"
    source.write_text("# Replacement\n\nStable contract.\n", encoding="utf-8")
    package = Path(import_source(source, workspace)["package"])
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "contract_version": 1, "action": "prepare",
        "workspace": str(workspace.config_path), "package": str(package),
    }), encoding="utf-8")
    replacement = Capability(**{
        **CAPABILITIES["source.notes"].__dict__,
        "implementation": "video_extract.capabilities:replacement_source_notes",
    })

    with patch.object(capability_module, "replacement_source_notes", capability_module.run_source_notes, create=True), \
            patch.dict(CAPABILITIES, {"source.notes": replacement}, clear=True):
        code, result = run_cli("capability", "run", "source.notes", "--request", str(request))

    assert code == 3
    assert result["status"] == "awaiting_model"
    assert result["provenance"]["implementation"].endswith(":replacement_source_notes")


def test_run_rejects_contract_mismatch_without_guessing_an_adapter(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"contract_version": 9}), encoding="utf-8")

    code, result = run_cli("capability", "run", "source.notes", "--request", str(request))

    assert code == 1
    assert result["status"] == "unsupported"
    assert result["validation"]["contract_version"] == "failed"
    assert result["next_action"]["maintenance_path"].endswith("video_extract/capabilities.py")
    assert "guess" not in json.dumps(result).lower()


def test_run_rejects_non_object_request_with_contract_response(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    request.write_text("[]", encoding="utf-8")

    code, result = run_cli("capability", "run", "source.notes", "--request", str(request))

    assert code == 3
    assert result["api_version"] == 1
    assert result["status"] == "missing_input"
    assert result["validation"]["request"] == "failed"


def test_workspace_doctor_includes_capability_and_is_read_only(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    agents_root = tmp_path / "isolated-agents"
    codex_root = tmp_path / "isolated-codex"

    code, result = run_cli(
        "workspace", "doctor", "--workspace", str(workspace.config_path),
        "--agents-root", str(agents_root), "--codex-root", str(codex_root),
    )

    assert code == 1
    assert result["capabilities"]["status"] == "completed"
    assert result["installation"]["status"] == "recoverable_failure"
    assert not agents_root.exists()
    assert not codex_root.exists()
