import contextlib
import io
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict
from unittest.mock import patch

import video_extract.capabilities as capability_module
from video_extract.capabilities import CAPABILITIES, Capability
from video_extract.cli import main, parser
from video_extract.source_import import import_source
from video_extract.workspace import WorkspaceConfig


def make_workspace(root: Path) -> WorkspaceConfig:
    root.mkdir(parents=True, exist_ok=True)
    (root / "project").mkdir()
    (root / "media").mkdir()
    (root / "vault").mkdir()
    config = root / "workspace.toml"
    config.write_text('''schema_version = 1
workspace_id = "11111111-1111-4111-8111-111111111111"
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
    assert [item["id"] for item in result["result"]["capabilities"]] == [
        "audio.mandarin", "learning.learn", "learning.practice", "media.acquire", "publish.netease", "source.notes"
    ]
    declared = next(item for item in result["result"]["capabilities"] if item["id"] == "source.notes")
    assert declared["contract_version"] == 1
    assert declared["input_type"] == "source-notes-request-v1"
    assert declared["output_type"] == "command-response-v1"
    assert declared["side_effect"] == "workspace_write"
    assert declared["authorization_category"] == "local_workspace"
    assert declared["recovery_query"] == "capability run source.notes with the same request"
    media = next(item for item in result["result"]["capabilities"] if item["id"] == "media.acquire")
    assert media["input_type"] == "media-acquire-request-v1"
    assert media["output_type"] == "command-response-v1"
    assert declared["implementation"] == "video_extract.capabilities:run_source_notes"

    learning = next(item for item in result["result"]["capabilities"] if item["id"] == "learning.learn")
    assert learning["input_type"] == "learning-request-v1"
    assert learning["implementation"] == "video_extract.capabilities:run_learning"
    practice = next(item for item in result["result"]["capabilities"] if item["id"] == "learning.practice")
    assert practice["input_type"] == "practice-request-v1"
    assert practice["implementation"] == "video_extract.practice:run_practice"


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


def test_check_reports_declared_dependency_state() -> None:
    with patch("video_extract.capabilities._dependency_available", return_value=False):
        code, result = run_cli("capability", "check", "source.notes")

    assert code == 3
    capability = result["result"]["capabilities"][0]
    assert capability["dependency_state"] == {"command:ffmpeg": False, "command:ffprobe": False, "python:PIL": False, "python:faster_whisper": False}
    assert result["status"] == "missing_dependency"
    assert result["validation"] == {"implementations": "passed", "dependencies": "failed", "contracts": "passed"}


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


def test_operation_identity_survives_relocation_and_tracks_contract_versions(tmp_path: Path) -> None:
    request_one = tmp_path / "one.json"
    request_two = tmp_path / "two.json"
    first_package = tmp_path / "first-package"
    second_package = tmp_path / "second-package"
    for package in (first_package, second_package):
        package.mkdir()
        (package / "manifest.json").write_text(json.dumps({"identity": "source-123"}), encoding="utf-8")
    base = {"contract_version": 1, "action": "prepare", "workspace": "/old/workspace",
            "package": str(first_package), "source_version": "v1"}
    request_one.write_text(json.dumps(base), encoding="utf-8")
    request_two.write_text(json.dumps({**base, "workspace": "/new/workspace", "package": str(second_package)}), encoding="utf-8")
    with patch.object(capability_module, "run_source_notes", return_value={"status": "awaiting_ai"}):
        _, first = run_cli("capability", "run", "source.notes", "--request", str(request_one))
        _, relocated = run_cli("capability", "run", "source.notes", "--request", str(request_two))
    upgraded = Capability(**{**CAPABILITIES["source.notes"].__dict__, "implementation_version": 2})
    with patch.object(capability_module, "run_source_notes", return_value={"status": "awaiting_ai"}), \
            patch.dict(CAPABILITIES, {"source.notes": upgraded}, clear=True):
        _, changed = run_cli("capability", "run", "source.notes", "--request", str(request_two))
    contract_request = tmp_path / "contract-two.json"
    contract_request.write_text(json.dumps({**base, "contract_version": 2,
                                             "workspace": "/new/workspace",
                                             "package": str(second_package)}), encoding="utf-8")
    contract_two = Capability(**{**CAPABILITIES["source.notes"].__dict__, "contract_version": 2})
    with patch.object(capability_module, "run_source_notes", return_value={"status": "awaiting_ai"}), \
            patch.dict(CAPABILITIES, {"source.notes": contract_two}, clear=True):
        _, contract_changed = run_cli("capability", "run", "source.notes", "--request", str(contract_request))

    assert first["operation_id"] == relocated["operation_id"]
    assert changed["operation_id"] == relocated["operation_id"]
    assert changed["provenance"]["implementation_version"] == 2
    assert contract_changed["operation_id"] != relocated["operation_id"]


def test_operation_identity_distinguishes_logical_workspaces(tmp_path: Path) -> None:
    first = make_workspace(tmp_path / "first")
    second = make_workspace(tmp_path / "second")
    second.config_path.write_text(second.config_path.read_text(encoding="utf-8").replace(
        'workspace_id = "11111111-1111-4111-8111-111111111111"',
        'workspace_id = "22222222-2222-4222-8222-222222222222"'), encoding="utf-8")
    package = tmp_path / "package"
    package.mkdir()
    (package / "manifest.json").write_text(json.dumps({"identity": "source-123"}), encoding="utf-8")
    requests = []
    for index, workspace in enumerate((first, second), 1):
        request = tmp_path / f"request-{index}.json"
        request.write_text(json.dumps({"contract_version": 1, "action": "prepare",
                                       "workspace": str(workspace.config_path),
                                       "package": str(package), "source_version": "v1"}), encoding="utf-8")
        requests.append(request)
    with patch.object(capability_module, "run_source_notes", capability_module.run_source_notes):
        _, one = run_cli("capability", "run", "source.notes", "--request", str(requests[0]))
        _, two = run_cli("capability", "run", "source.notes", "--request", str(requests[1]))

    assert one["operation_id"] != two["operation_id"]


def test_check_rejects_incompatible_contract_declaration_and_callable() -> None:
    def incompatible() -> list[str]:
        return []

    bad = Capability(**{**CAPABILITIES["source.notes"].__dict__, "input_type": "unknown-v9",
                        "implementation": "video_extract.capabilities:incompatible"})
    with patch.object(capability_module, "incompatible", incompatible, create=True), \
            patch.dict(CAPABILITIES, {"source.notes": bad}, clear=True):
        code, result = run_cli("capability", "check", "source.notes")

    assert code == 1
    checked = result["result"]["capabilities"][0]
    assert checked["contract_state"] == "incompatible"
    assert result["validation"]["contracts"] == "failed"


def test_contract_failure_with_dependencies_present_is_unsupported() -> None:
    def incompatible(_request: dict) -> dict:
        return {"status": "complete"}

    bad = Capability(**{**CAPABILITIES["source.notes"].__dict__,
                        "implementation": "video_extract.capabilities:incompatible"})
    with patch.object(capability_module, "incompatible", incompatible, create=True), \
            patch("video_extract.capabilities._dependency_available", return_value=True), \
            patch.dict(CAPABILITIES, {"source.notes": bad}, clear=True):
        code, result = run_cli("capability", "check", "source.notes")

    assert code == 1
    assert result["status"] == "unsupported"
    assert result["validation"] == {"implementations": "passed", "dependencies": "passed", "contracts": "failed"}


def test_contract_failure_precedes_missing_dependency() -> None:
    def incompatible(_request: dict) -> list[str]:
        return []

    incompatible.__capability_contract__ = {"input_type": "source-notes-request-v1",
                                            "output_type": "command-response-v1"}
    bad = Capability(**{**CAPABILITIES["source.notes"].__dict__,
                        "implementation": "video_extract.capabilities:incompatible"})
    with patch.object(capability_module, "incompatible", incompatible, create=True), \
            patch("video_extract.capabilities._dependency_available", return_value=False), \
            patch.dict(CAPABILITIES, {"source.notes": bad}, clear=True):
        code, result = run_cli("capability", "check", "source.notes")

    assert code == 1
    assert result["status"] == "unsupported"
    assert result["validation"] == {"implementations": "passed", "dependencies": "failed", "contracts": "failed"}


def test_metadata_cannot_hide_incompatible_return_annotation() -> None:
    def disguised(_request: dict) -> list[str]:
        return []

    disguised.__capability_contract__ = {"input_type": "source-notes-request-v1",
                                         "output_type": "command-response-v1"}
    bad = Capability(**{**CAPABILITIES["source.notes"].__dict__,
                        "implementation": "video_extract.capabilities:disguised"})
    with patch.object(capability_module, "disguised", disguised, create=True), \
            patch.dict(CAPABILITIES, {"source.notes": bad}, clear=True):
        code, result = run_cli("capability", "check", "source.notes")

    assert code == 1
    assert result["result"]["capabilities"][0]["contract_state"] == "incompatible"


def test_metadata_cannot_hide_incompatible_request_annotation() -> None:
    def disguised(_request: list[str]) -> dict:
        return {"status": "complete"}

    disguised.__capability_contract__ = {"input_type": "source-notes-request-v1",
                                         "output_type": "command-response-v1"}
    bad = Capability(**{**CAPABILITIES["source.notes"].__dict__,
                        "implementation": "video_extract.capabilities:disguised_input"})
    with patch.object(capability_module, "disguised_input", disguised, create=True), \
            patch.dict(CAPABILITIES, {"source.notes": bad}, clear=True):
        code, result = run_cli("capability", "check", "source.notes")

    assert code == 1
    assert result["result"]["capabilities"][0]["contract_state"] == "incompatible"


def test_typed_dict_output_annotation_is_compatible() -> None:
    class AdapterResult(TypedDict):
        status: str

    def compatible(_request: dict) -> AdapterResult:
        return {"status": "complete"}

    compatible.__capability_contract__ = {"input_type": "source-notes-request-v1",
                                          "output_type": "command-response-v1"}
    replacement = Capability(**{**CAPABILITIES["source.notes"].__dict__,
                                "implementation": "video_extract.capabilities:typed_dict_adapter"})
    with patch.object(capability_module, "typed_dict_adapter", compatible, create=True), \
            patch.dict(CAPABILITIES, {"source.notes": replacement}, clear=True):
        code, result = run_cli("capability", "check", "source.notes")

    assert code == 0
    assert result["result"]["capabilities"][0]["contract_state"] == "compatible"


def test_mapping_protocol_input_and_mapping_subclass_output_are_compatible() -> None:
    class ResultMap(dict[str, object]):
        pass

    def compatible(_request: Mapping[str, object]) -> ResultMap:
        return ResultMap(status="complete")

    compatible.__capability_contract__ = {"input_type": "source-notes-request-v1",
                                          "output_type": "command-response-v1"}
    replacement = Capability(**{**CAPABILITIES["source.notes"].__dict__,
                                "implementation": "video_extract.capabilities:mapping_subclass_adapter"})
    with patch.object(capability_module, "mapping_subclass_adapter", compatible, create=True), \
            patch.dict(CAPABILITIES, {"source.notes": replacement}, clear=True):
        code, result = run_cli("capability", "check", "source.notes")

    assert code == 0
    assert result["result"]["capabilities"][0]["contract_state"] == "compatible"


def test_specific_request_mapping_subclass_is_rejected_without_execution() -> None:
    executed = False

    class RequestMap(dict[str, object]):
        def specialized(self) -> str:
            return "only-on-subclass"

    def incompatible(request: RequestMap) -> dict:
        nonlocal executed
        executed = True
        return {"status": request.specialized()}

    incompatible.__capability_contract__ = {"input_type": "source-notes-request-v1",
                                            "output_type": "command-response-v1"}
    replacement = Capability(**{**CAPABILITIES["source.notes"].__dict__,
                                "implementation": "video_extract.capabilities:specific_request_adapter"})
    with patch.object(capability_module, "specific_request_adapter", incompatible, create=True), \
            patch.dict(CAPABILITIES, {"source.notes": replacement}, clear=True):
        code, result = run_cli("capability", "check", "source.notes")

    assert code == 1
    assert result["status"] == "unsupported"
    assert result["result"]["capabilities"][0]["contract_state"] == "incompatible"
    assert executed is False


def test_run_incompatible_return_keeps_v1_envelope(tmp_path: Path) -> None:
    # Unannotated adapters are allowed by the probe protocol, so runtime must still enforce Mapping.
    def returns_list(_request: dict):
        return []

    request = tmp_path / "request.json"
    request.write_text(json.dumps({"contract_version": 1}), encoding="utf-8")
    replacement = Capability(**{**CAPABILITIES["source.notes"].__dict__,
                                "implementation": "video_extract.capabilities:returns_list"})
    returns_list.__capability_contract__ = {"input_type": "source-notes-request-v1",
                                            "output_type": "command-response-v1"}
    with patch.object(capability_module, "returns_list", returns_list, create=True), \
            patch.dict(CAPABILITIES, {"source.notes": replacement}, clear=True):
        code, result = run_cli("capability", "run", "source.notes", "--request", str(request))

    assert code == 1
    assert result["api_version"] == 1
    assert result["status"] == "failed"
    assert result["validation"]["output_type"] == "failed"


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
    assert set(result) == {"api_version", "workspace_id", "operation_id", "status", "observed_revision", "result", "artifact_refs", "validation", "provenance", "next_action", "diagnostics"}
    assert result["result"]["capabilities"]["status"] == "completed"
    assert result["result"]["installation"]["status"] == "recoverable_failure"
    assert not agents_root.exists()
    assert not codex_root.exists()


def test_invalid_workspace_is_a_normalized_missing_input_response(tmp_path: Path) -> None:
    code, result = run_cli(
        "workspace", "doctor", "--workspace", str(tmp_path / "missing.toml"),
        "--agents-root", str(tmp_path / "agents"), "--codex-root", str(tmp_path / "codex"),
    )

    assert code == 3
    assert result["api_version"] == 1
    assert result["status"] == "missing_input"
    assert result["validation"]["workspace"] == "failed"


def test_workspace_identity_survives_workspace_relocation(tmp_path: Path) -> None:
    first = tmp_path / "first"
    workspace = make_workspace(first)
    first_agents, first_codex = tmp_path / "agents-a", tmp_path / "codex-a"
    _, before = run_cli("workspace", "doctor", "--workspace", str(workspace.config_path),
                        "--agents-root", str(first_agents), "--codex-root", str(first_codex))
    moved = tmp_path / "moved"
    first.rename(moved)
    with (moved / "workspace.toml").open("a", encoding="utf-8") as stream:
        stream.write('\n[tools.pyvideotrans]\npython = "tools/python"\ncli = "tools/cli.py"\n')
    _, after = run_cli("workspace", "doctor", "--workspace", str(moved / "workspace.toml"),
                       "--agents-root", str(first_agents), "--codex-root", str(first_codex))

    assert before["workspace_id"] == after["workspace_id"]


def test_legacy_workspace_doctor_requires_persistent_identity(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    text = workspace.config_path.read_text(encoding="utf-8").replace(
        'workspace_id = "11111111-1111-4111-8111-111111111111"\n', '')
    workspace.config_path.write_text(text, encoding="utf-8")

    code, result = run_cli("workspace", "doctor", "--workspace", str(workspace.config_path),
                           "--agents-root", str(tmp_path / "agents"),
                           "--codex-root", str(tmp_path / "codex"))

    assert code == 3
    assert result["status"] == "missing_input"
    assert "workspace_id" in result["diagnostics"][0]


def test_workspace_template_identity_is_rejected(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.config_path.write_text(workspace.config_path.read_text(encoding="utf-8").replace(
        'workspace_id = "11111111-1111-4111-8111-111111111111"',
        'workspace_id = "REPLACE_ME_WITH_UUID"'), encoding="utf-8")

    code, result = run_cli("workspace", "doctor", "--workspace", str(workspace.config_path),
                           "--agents-root", str(tmp_path / "agents"),
                           "--codex-root", str(tmp_path / "codex"))

    assert code == 3
    assert result["status"] == "missing_input"
    assert "placeholder" in result["diagnostics"][0]


def test_legacy_legal_workspace_ids_remain_readable_for_show_and_source_import(tmp_path: Path) -> None:
    for index, legal_id in enumerate(("fixture-course", "example-notes", "default-vault", "todoist-study")):
        root = tmp_path / str(index)
        workspace = make_workspace(root)
        workspace.config_path.write_text(workspace.config_path.read_text(encoding="utf-8").replace(
            'workspace_id = "11111111-1111-4111-8111-111111111111"',
            f'workspace_id = "{legal_id}"'), encoding="utf-8")
        show_code, shown = run_cli("workspace", "show", "--workspace", str(workspace.config_path))
        source = root / "source.md"
        source.write_text("# Source\n", encoding="utf-8")
        import_code, imported = run_cli("source", "import", str(source),
                                        "--workspace", str(workspace.config_path))

        assert show_code == 0
        assert shown["workspace_id"] == legal_id
        assert import_code == 0
        assert imported["status"] == "ready"


def test_only_exact_workspace_template_sentinel_is_rejected(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.config_path.write_text(workspace.config_path.read_text(encoding="utf-8").replace(
        'workspace_id = "11111111-1111-4111-8111-111111111111"',
        'workspace_id = "replace-me-course"'), encoding="utf-8")

    code, shown = run_cli("workspace", "show", "--workspace", str(workspace.config_path))

    assert code == 0
    assert shown["workspace_id"] == "replace-me-course"


def test_malformed_workspace_toml_is_normalized(tmp_path: Path) -> None:
    malformed = tmp_path / "workspace.toml"
    malformed.write_text("schema_version = [", encoding="utf-8")

    code, result = run_cli("workspace", "doctor", "--workspace", str(malformed),
                           "--agents-root", str(tmp_path / "agents"),
                           "--codex-root", str(tmp_path / "codex"))

    assert code == 3
    assert result["api_version"] == 1
    assert result["status"] == "missing_input"


def test_capability_invalid_workspace_is_normalized(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"contract_version": 1, "action": "prepare",
                                   "workspace": str(tmp_path / "missing.toml"),
                                   "package": str(tmp_path / "package")}), encoding="utf-8")

    code, result = run_cli("capability", "run", "source.notes", "--request", str(request))

    assert code == 3
    assert result["status"] == "missing_input"
    assert result["validation"]["request"] == "failed"
