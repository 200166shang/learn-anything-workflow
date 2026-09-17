import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import video_extract.authoritative_notes as notes_module

from video_extract.authoritative_notes import associate_sources, audit_notes, finalize_note, prepare_note, reconcile_notes
from video_extract.workspace import WorkspaceError
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
        "association": {"status": "not_applicable", "evidence": None},
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
    assert association["result"]["association"]["status"] == "unverified"
    assert association["validation"]["association_evidence"] == "insufficient"


def test_association_verifies_only_structured_digest_equality(tmp_path: Path) -> None:
    config = workspace(tmp_path / "ws")
    first = tmp_path / "one.srt"; first.write_text("1\n00:00:01,000 --> 00:00:02,000\nOne\n", encoding="utf-8")
    second = tmp_path / "two.srt"; second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
    one = register(config, first)["result"]; two = register(config, second)["result"]
    evidence = {"type": "content_digest_equality", "source_id": one["source_id"], "source_version": one["source_version"],
                "related_source_id": two["source_id"], "related_source_version": two["source_version"],
                "source_content_sha256": one["version_basis"]["content_sha256"],
                "related_content_sha256": two["version_basis"]["content_sha256"]}

    verified = associate_sources(config, one["source_id"], two["source_id"], evidence)
    arbitrary = associate_sources(config, one["source_id"], two["source_id"], {"type": "user_claim", "text": "same"})

    assert verified["result"]["association"]["status"] == "verified"
    assert arbitrary["result"]["association"]["status"] == "unverified"


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
    with pytest.raises(WorkspaceError, match="not on the current commit chain"):
        audit_notes(config)
    target = failed["next_action"]["commit_id"]
    recovered_run = subprocess.run(
        [sys.executable, "-m", "video_extract.cli", "notes", "reconcile", "--commit-id", target,
         "--workspace", str(config.config_path), "--json"], capture_output=True, text=True)
    recovered = json.loads(recovered_run.stdout)
    current = audit_notes(config)
    assert recovered_run.returncode == 0
    assert recovered["status"] == "completed" and recovered["result"]["commit_id"] == target
    assert current["result"]["notes"][0]["revision"] == 2
    assert Path(current["result"]["notes"][0]["note"]).read_text(encoding="utf-8").startswith("# Second")


def test_history_revision_pins_every_reconstructable_artifact(tmp_path: Path) -> None:
    config = workspace(tmp_path / "ws")
    source = tmp_path / "fixture.md"; source.write_text("One\n\nTwo\n", encoding="utf-8")
    registered = register(config, source); prepared = prepare_note(config, registered["result"]["source_id"])
    finalize_note(config, draft(tmp_path / "first.json", prepared, "# First\n\nTwo\n"))
    finalize_note(config, draft(tmp_path / "second.json", prepared, "# Second\n\nTwo\n", expected_revision=1))
    pointer = json.loads((config.results / "source-notes/current.json").read_text())
    commit = json.loads((config.results / "source-notes/commits" / f"{pointer['commit_id']}.json").read_text())
    history = commit["notes"][registered["result"]["source_id"]]["history"]

    assert len(history) == 2
    assert all(set(entry) >= {"body_object", "citations_object", "corrections_object",
                              "attachment_objects", "association", "source_version"} for entry in history)


def test_finalize_rejects_imprecise_or_malformed_locators_with_command_response(tmp_path: Path) -> None:
    config = workspace(tmp_path / "ws")
    source = tmp_path / "fixture.md"; source.write_text("# Exact heading\n\nExact paragraph.\n", encoding="utf-8")
    registered = register(config, source); prepared = prepare_note(config, registered["result"]["source_id"])
    request = draft(tmp_path / "bad.json", prepared, "# Note\n")
    value = json.loads(request.read_text()); value["citations"] = ["not-an-object"]
    request.write_text(json.dumps(value))
    command = [sys.executable, "-m", "video_extract.cli", "notes", "finalize", registered["result"]["source_id"],
               "--request", str(request), "--workspace", str(config.config_path), "--json"]

    completed = subprocess.run(command, capture_output=True, text=True)
    result = json.loads(completed.stdout)

    assert completed.returncode == 3
    assert result["api_version"] == 1 and result["status"] == "missing_input"
    assert result["validation"] == {"request": "failed"}


def test_locators_match_exact_structured_boundaries(tmp_path: Path) -> None:
    config = workspace(tmp_path / "ws")
    document = tmp_path / "fixture.md"; document.write_text("# Exact heading\n\nExact paragraph.\n", encoding="utf-8")
    registered = register(config, document); prepared = prepare_note(config, registered["result"]["source_id"])
    request = draft(tmp_path / "heading.json", prepared, "# Note\n")
    value = json.loads(request.read_text()); value["citations"] = [{"claim": "x", "locator_type": "heading", "locator": "Exact"}]
    request.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="heading citation"):
        finalize_note(config, request)

    for invalid in ("paragraph:0", "paragraph:-1", "paragraph:01"):
        value["citations"] = [{"claim": "x", "locator_type": "paragraph", "locator": invalid}]
        request.write_text(json.dumps(value))
        with pytest.raises(ValueError, match="paragraph citation"):
            finalize_note(config, request)

    transcript = tmp_path / "fixture.srt"; transcript.write_text("1\n00:00:03,000 --> 00:00:05,000\nEvidence\n", encoding="utf-8")
    srt = register(config, transcript); srt_prepared = prepare_note(config, srt["result"]["source_id"])
    bad_srt = draft(tmp_path / "srt.json", srt_prepared, "# Note\n")
    srt_value = json.loads(bad_srt.read_text()); srt_value["citations"] = [{"claim": "x", "locator_type": "timestamp", "locator": "00:00:03,000"}]
    bad_srt.write_text(json.dumps(srt_value))
    with pytest.raises(ValueError, match="timestamp citation"):
        finalize_note(config, bad_srt)


def test_reconcile_does_not_confirm_when_any_object_barrier_fails(tmp_path: Path) -> None:
    config = workspace(tmp_path / "ws")
    source = tmp_path / "fixture.md"; source.write_text("One\n\nTwo\n", encoding="utf-8")
    registered = register(config, source); prepared = prepare_note(config, registered["result"]["source_id"])
    finalize_note(config, draft(tmp_path / "draft.json", prepared, "# Note\n\nTwo\n"))

    with patch("video_extract.authoritative_notes._sync_file", side_effect=OSError("barrier failed")):
        with pytest.raises(OSError, match="barrier failed"):
            reconcile_notes(config)


@pytest.mark.parametrize("failure_stage", ["commit_directory_fsync", "pointer_write", "pointer_directory_fsync"])
def test_real_publish_io_failure_returns_executable_target_recovery(tmp_path: Path, failure_stage: str) -> None:
    config = workspace(tmp_path / failure_stage)
    source = tmp_path / f"{failure_stage}.md"; source.write_text("One\n\nTwo\n", encoding="utf-8")
    registered = register(config, source); prepared = prepare_note(config, registered["result"]["source_id"])
    request = draft(tmp_path / f"{failure_stage}.json", prepared, "# Note\n\nTwo\n")
    original_sync = notes_module._sync_dir
    original_write = notes_module.atomic_write_json

    def failing_sync(path: Path) -> None:
        if failure_stage == "commit_directory_fsync" and path.name == "commits":
            raise OSError("real commit dir failure")
        if failure_stage == "pointer_directory_fsync" and path.name == "source-notes":
            raise OSError("real pointer dir failure")
        original_sync(path)

    def failing_write(path: Path, value: dict) -> None:
        if failure_stage == "pointer_write" and path.name == "current.json":
            raise OSError("real pointer write failure")
        original_write(path, value)

    with patch.object(notes_module, "_sync_dir", side_effect=failing_sync), \
            patch.object(notes_module, "atomic_write_json", side_effect=failing_write):
        failed = finalize_note(config, request)

    target = failed["next_action"]["commit_id"]
    assert failed["status"] == "recoverable_failure"
    assert target and failed["next_action"]["stage"] == failure_stage
    recovered = reconcile_notes(config, target)
    assert recovered["status"] == "completed" and recovered["result"]["commit_id"] == target


def test_reconcile_conflicts_when_finalize_wins_writer_race(tmp_path: Path, monkeypatch) -> None:
    config = workspace(tmp_path / "ws")
    source = tmp_path / "fixture.md"; source.write_text("One\n\nTwo\n", encoding="utf-8")
    registered = register(config, source); prepared = prepare_note(config, registered["result"]["source_id"])
    finalize_note(config, draft(tmp_path / "first.json", prepared, "# First\n\nTwo\n"))
    monkeypatch.setenv("VIDEO_EXTRACT_NOTES_TEST_FAULT", "before_publish")
    interrupted = finalize_note(config, draft(tmp_path / "orphan.json", prepared, "# Orphan\n\nTwo\n", expected_revision=1))
    monkeypatch.delenv("VIDEO_EXTRACT_NOTES_TEST_FAULT")
    target = interrupted["next_action"]["commit_id"]

    winner = finalize_note(config, draft(tmp_path / "winner.json", prepared, "# Winner\n\nTwo\n", expected_revision=1))
    conflict = reconcile_notes(config, target)
    pointer = json.loads((config.results / "source-notes/current.json").read_text())

    assert winner["status"] == "completed"
    assert conflict["status"] == "awaiting_user" and conflict["validation"]["expected_revision"] == "conflict"
    assert pointer["commit_id"] == winner["result"]["commit_id"]
    assert Path(conflict["result"]["candidate"]).is_file()


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
