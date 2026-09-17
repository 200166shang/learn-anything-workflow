import json
import os
import subprocess
import sys
from pathlib import Path

from video_extract.authoritative_notes import associate_sources, audit_notes, finalize_note, prepare_note
from video_extract.source_registry import register
from video_extract.workspace import WorkspaceConfig


def workspace(root: Path) -> WorkspaceConfig:
    root.mkdir()
    config = root / "workspace.toml"
    config.write_text('''schema_version = 2
workspace_id = "11111111-1111-4111-8111-111111111111"
[paths]
project = "project"
results = "results"
sources = "sources"
derived = "derived"
local = "local"
''', encoding="utf-8")
    return WorkspaceConfig.load(config)


def draft(path: Path, prepared: dict, body: str, *, expected_revision: int = 0) -> Path:
    request = {
        "schema_version": 1,
        "source_id": prepared["result"]["source_id"],
        "source_version": prepared["result"]["source_version"],
        "expected_revision": expected_revision,
        "markdown": body,
        "citations": [{"claim": "输入经阶段产生输出", "locator": "paragraph:2", "locator_type": "paragraph"}],
        "corrections": [],
        "attachments": [],
        "association": {"status": "not_applicable", "evidence": "standalone document"},
    }
    path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    return path


def test_document_prepare_pauses_for_model_with_paragraph_locators(tmp_path: Path) -> None:
    config = workspace(tmp_path / "ws")
    source = tmp_path / "fixture.md"
    source.write_text("# 原理\n\n输入经阶段产生输出。\n", encoding="utf-8")
    registered = register(config, source)

    result = prepare_note(config, registered["result"]["source_id"], registered["result"]["source_version"])

    assert result["status"] == "awaiting_model"
    assert result["next_action"]["type"] == "model"
    text = Path(result["result"]["model_input"]).read_text(encoding="utf-8")
    assert "paragraph:2" in text
    assert "-->" not in text
    assert str(config.results) not in result["result"]["model_input"]


def test_srt_prepare_keeps_timestamps_but_marks_video_association_unverified(tmp_path: Path) -> None:
    config = workspace(tmp_path / "ws")
    source = tmp_path / "fixture.srt"
    source.write_text("1\n00:00:03,000 --> 00:00:05,000\nEvidence\n", encoding="utf-8")
    registered = register(config, source)

    result = prepare_note(config, registered["result"]["source_id"])

    model_input = Path(result["result"]["model_input"]).read_text(encoding="utf-8")
    assert "00:00:03,000 --> 00:00:05,000" in model_input
    assert "unverified_video_association" in model_input


def test_source_association_never_claims_matching_media_without_evidence(tmp_path: Path) -> None:
    config = workspace(tmp_path / "ws")
    first = tmp_path / "one.srt"; first.write_text("1\n00:00:01,000 --> 00:00:02,000\nOne\n", encoding="utf-8")
    second = tmp_path / "two.txt"; second.write_text("Two\n", encoding="utf-8")
    one = register(config, first)["result"]["source_id"]
    two = register(config, second)["result"]["source_id"]

    association = associate_sources(config, one, two, None)

    assert association["status"] == "completed"
    assert association["result"]["association"]["status"] == "insufficient_evidence"
    assert association["validation"]["association_evidence"] == "insufficient"


def test_finalize_publishes_body_citations_and_history_as_one_deeply_valid_commit(tmp_path: Path) -> None:
    config = workspace(tmp_path / "ws")
    source = tmp_path / "fixture.md"
    source.write_text("# 原理\n\n输入经阶段产生输出。\n", encoding="utf-8")
    registered = register(config, source)
    prepared = prepare_note(config, registered["result"]["source_id"])
    request = draft(tmp_path / "draft.json", prepared, "# 权威笔记\n\n输入经阶段产生输出。\n")

    completed = finalize_note(config, request)
    audited = audit_notes(config)

    assert completed["status"] == "completed"
    assert completed["result"]["revision"] == 1
    assert completed["result"]["history_length"] == 1
    assert audited["status"] == "completed"
    assert audited["validation"] == {"commits": "passed", "objects": "passed", "sources": "passed"}
    note = Path(completed["result"]["note"])
    assert note.is_file() and note.read_text(encoding="utf-8").startswith("# 权威笔记")
    assert config.results in note.parents


def test_revision_conflict_preserves_candidate_without_changing_current(tmp_path: Path) -> None:
    config = workspace(tmp_path / "ws")
    source = tmp_path / "fixture.md"; source.write_text("One\n\nTwo\n", encoding="utf-8")
    registered = register(config, source); prepared = prepare_note(config, registered["result"]["source_id"])
    first = finalize_note(config, draft(tmp_path / "first.json", prepared, "# First\n\nTwo\n"))
    conflict = finalize_note(config, draft(tmp_path / "second.json", prepared, "# Second\n\nTwo\n", expected_revision=0))

    assert first["status"] == "completed"
    assert conflict["status"] == "awaiting_user"
    assert Path(conflict["result"]["candidate"]).is_file()
    assert audit_notes(config)["result"]["notes"][0]["revision"] == 1


def test_failure_before_publish_leaves_previous_note_current(tmp_path: Path, monkeypatch) -> None:
    config = workspace(tmp_path / "ws")
    source = tmp_path / "fixture.md"; source.write_text("One\n\nTwo\n", encoding="utf-8")
    registered = register(config, source); prepared = prepare_note(config, registered["result"]["source_id"])
    finalize_note(config, draft(tmp_path / "first.json", prepared, "# First\n\nTwo\n"))
    monkeypatch.setenv("VIDEO_EXTRACT_NOTES_TEST_FAULT", "before_publish")
    failed = finalize_note(config, draft(tmp_path / "second.json", prepared, "# Second\n\nTwo\n", expected_revision=1))
    monkeypatch.delenv("VIDEO_EXTRACT_NOTES_TEST_FAULT")

    assert failed["status"] == "recoverable_failure"
    current = audit_notes(config)
    assert current["result"]["notes"][0]["revision"] == 1
    assert Path(current["result"]["notes"][0]["note"]).read_text(encoding="utf-8").startswith("# First")


def test_identical_finalize_reuses_valid_revision(tmp_path: Path) -> None:
    config = workspace(tmp_path / "ws")
    source = tmp_path / "fixture.md"; source.write_text("One\n\nTwo\n", encoding="utf-8")
    registered = register(config, source); prepared = prepare_note(config, registered["result"]["source_id"])
    request = draft(tmp_path / "draft.json", prepared, "# Stable\n\nTwo\n")
    first = finalize_note(config, request)
    value = json.loads(request.read_text(encoding="utf-8")); value["expected_revision"] = 1
    request.write_text(json.dumps(value), encoding="utf-8")

    reused = finalize_note(config, request)

    assert reused["status"] == "completed"
    assert reused["result"]["action"] == "reused"
    assert reused["result"]["revision"] == 1
    assert audit_notes(config)["result"]["commit_count"] == 1


def test_v2_cli_prepare_and_finalize_use_command_response_exit_semantics(tmp_path: Path) -> None:
    config = workspace(tmp_path / "ws")
    source = tmp_path / "fixture.md"; source.write_text("One\n\nTwo\n", encoding="utf-8")
    registered = register(config, source)
    command = [sys.executable, "-m", "video_extract.cli"]
    prepared_run = subprocess.run(command + ["notes", "prepare", registered["result"]["source_id"],
        "--workspace", str(config.config_path), "--json"], capture_output=True, text=True)
    prepared = json.loads(prepared_run.stdout)
    request = draft(tmp_path / "draft.json", prepared, "# CLI\n\nTwo\n")
    finalized_run = subprocess.run(command + ["notes", "finalize", registered["result"]["source_id"],
        "--request", str(request), "--workspace", str(config.config_path), "--json"], capture_output=True, text=True)
    finalized = json.loads(finalized_run.stdout)

    assert prepared_run.returncode == 3 and prepared["status"] == "awaiting_model"
    assert finalized_run.returncode == 0 and finalized["status"] == "completed"
