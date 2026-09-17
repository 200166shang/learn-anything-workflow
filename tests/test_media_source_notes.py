import json
import pytest
from pathlib import Path

from video_extract.media_note_operations import (
    register_frame_adapter,
    register_transcript_adapter,
    prepare_media_note,
)
from video_extract.media_operations import ensure_request, register_media_adapter
from video_extract.capabilities import run_capability, run_source_notes
from video_extract.authoritative_notes import audit_notes, finalize_note
from video_extract.source_registry import register
from video_extract.workspace import WorkspaceConfig


WORKSPACE = '''schema_version = 2
workspace_id = "11111111-1111-4111-8111-111111111111"
[paths]
project = "project"
results = "results"
sources = "sources"
derived = "derived"
local = "local"
'''


class MediaFixture:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def acquire(self, item, kinds, target, **kwargs):
        self.calls.append(list(kinds)); target.mkdir(parents=True, exist_ok=True)
        outputs = {}
        for kind in kinds:
            output = target / f"{kind}.fixture"
            output.write_bytes(self.payloads[kind]); outputs[kind] = output
        return {"artifacts": outputs, "query_handle": kwargs["idempotency_token"]}

    def reconcile(self, attempt, target):
        return {"state": "retry_safe", "query_handle": attempt["query_handle"]}


class TranscriptFixture:
    def __init__(self): self.calls = 0

    def transcribe(self, media, target, *, language, idempotency_token):
        self.calls += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("1\n00:00:01,000 --> 00:00:02,000\n真实转写\n", encoding="utf-8")
        return {"engine": "fixture-asr", "language": language}


class FrameFixture:
    def __init__(self): self.calls = []

    def extract(self, video, target, *, timestamp_ms, idempotency_token):
        self.calls.append((video.read_bytes(), timestamp_ms))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x89PNG\r\n\x1a\nREAL-FRAME-" + str(timestamp_ms).encode())
        return {"decoder": "fixture-frame"}


def setup_operation(tmp_path: Path, media: list[str]):
    config_path = tmp_path / "workspace.toml"; config_path.write_text(WORKSPACE)
    config = WorkspaceConfig.load(config_path)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"items": [{"id": "chapter-1", "source": "fixture://chapter-1",
                                                "adapter": "fixture.media-notes-v1"}]}))
    source = register(config, catalog)["result"]
    request = {"contract_version": 1, "workspace": str(config_path), "source_id": source["source_id"],
               "source_version": source["source_version"], "scope": ["chapter-1"], "media": media,
               "language": "zh", "quality": "standard"}
    acquired = ensure_request(request)
    return config, acquired


def test_audio_prepare_transcribes_once_and_reuses_registered_transcript(tmp_path: Path):
    media = MediaFixture({"audio": b"fixture audio"}); register_media_adapter("fixture.media-notes-v1", media)
    transcript = TranscriptFixture(); register_transcript_adapter("fixture.transcript-v1", transcript)
    config, acquired = setup_operation(tmp_path, ["audio"])

    first = prepare_media_note(config, acquired["operation_id"], "chapter-1",
                               transcript_adapter="fixture.transcript-v1")
    second = prepare_media_note(config, acquired["operation_id"], "chapter-1",
                                transcript_adapter="fixture.transcript-v1")

    assert first["status"] == second["status"] == "awaiting_model"
    assert transcript.calls == 1
    assert first["result"]["evidence"] == {"kind": "audio_only", "candidates": []}
    assert first["result"]["source_id"] == second["result"]["source_id"]
    assert "真实转写" in Path(first["result"]["model_input"]).read_text(encoding="utf-8")


def test_video_prepare_extracts_real_frame_candidates_separately_and_repairs_only_missing_frame(tmp_path: Path):
    media = MediaFixture({"video": b"fixture video bytes", "subtitles":
                          b"1\n00:00:01,000 --> 00:00:02,000\nFrame claim\n"})
    register_media_adapter("fixture.media-notes-v1", media)
    frames = FrameFixture(); register_frame_adapter("fixture.frames-v1", frames)
    config, acquired = setup_operation(tmp_path, ["subtitles", "video"])

    first = prepare_media_note(config, acquired["operation_id"], "chapter-1",
                               frame_adapter="fixture.frames-v1")
    candidate = Path(first["result"]["evidence"]["candidates"][0]["image"])
    candidate.unlink()
    second = prepare_media_note(config, acquired["operation_id"], "chapter-1",
                                frame_adapter="fixture.frames-v1")

    assert first["status"] == second["status"] == "awaiting_model"
    assert first["next_action"]["action"] == "select visual evidence"
    assert len(frames.calls) == 2 and all(call[0] == b"fixture video bytes" for call in frames.calls)
    assert candidate.is_file()
    assert first["result"]["evidence"]["review_mode"] == "unreviewed"
    assert first["result"]["evidence"]["candidates"][0]["source_video_sha256"]


def test_video_without_actual_video_pauses_visual_evidence_without_fabricating_frames(tmp_path: Path):
    media = MediaFixture({"subtitles": b"1\n00:00:01,000 --> 00:00:02,000\nClaim\n"})
    register_media_adapter("fixture.media-notes-v1", media)
    frames = FrameFixture(); register_frame_adapter("fixture.frames-v1", frames)
    config, acquired = setup_operation(tmp_path, ["subtitles"])

    result = prepare_media_note(config, acquired["operation_id"], "chapter-1",
                                frame_adapter="fixture.frames-v1", require_visuals=True)

    assert result["status"] == "missing_input"
    assert result["result"]["available"] == ["transcript"]
    assert result["next_action"]["missing"] == "video"
    assert frames.calls == []


def test_model_selection_is_not_mislabeled_human_and_only_approved_real_frame_is_adoptable(tmp_path: Path):
    media = MediaFixture({"video": b"video", "subtitles":
                          b"1\n00:00:01,000 --> 00:00:02,000\nClaim\n"})
    register_media_adapter("fixture.media-notes-v1", media)
    register_frame_adapter("fixture.frames-v1", FrameFixture())
    config, acquired = setup_operation(tmp_path, ["subtitles", "video"])
    prepared = prepare_media_note(config, acquired["operation_id"], "chapter-1",
                                  frame_adapter="fixture.frames-v1")
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"review_mode": "human", "approved": ["frame-001"]}))

    selected = prepare_media_note(config, acquired["operation_id"], "chapter-1",
                                  frame_adapter="fixture.frames-v1", selection=selection)

    assert selected["result"]["evidence"]["review_mode"] == "model_only"
    assert selected["result"]["evidence"]["approved"][0]["candidate_id"] == "frame-001"
    request_template = json.loads(Path(selected["result"]["finalize_request"]).read_text())
    assert request_template["attachments"] == [prepared["result"]["evidence"]["candidates"][0]["image"]]
    assert "frame-001" in request_template["markdown"]


def test_source_notes_capability_exposes_media_preparation_public_seam(tmp_path: Path):
    register_media_adapter("fixture.media-notes-v1", MediaFixture({"audio": b"audio"}))
    transcript = TranscriptFixture(); register_transcript_adapter("fixture.transcript-v1", transcript)
    config, acquired = setup_operation(tmp_path, ["audio"])

    result = run_source_notes({"workspace": str(config.config_path), "action": "prepare",
                               "media_operation_id": acquired["operation_id"], "item_id": "chapter-1",
                               "transcript_adapter": "fixture.transcript-v1"})

    assert result["status"] == "awaiting_model"
    assert result["operation_id"] == acquired["operation_id"]
    assert result["provenance"]["media_source_version"]


def test_source_notes_capability_keeps_registered_document_prepare_path(tmp_path: Path):
    config_path = tmp_path / "workspace.toml"; config_path.write_text(WORKSPACE)
    config = WorkspaceConfig.load(config_path)
    document = tmp_path / "source.md"; document.write_text("# Fact\n\nEvidence\n")
    source = register(config, document)["result"]

    result = run_source_notes({"workspace": str(config.config_path), "action": "prepare",
                               "source_id": source["source_id"], "source_version": source["source_version"]})

    assert result["status"] == "awaiting_ai"
    assert result["action"] == "notes_write"
    assert result["source_id"] == source["source_id"]


def test_finalize_keeps_adopted_real_frame_readable_after_original_media_is_missing(tmp_path: Path):
    register_media_adapter("fixture.media-notes-v1", MediaFixture({"video": b"video", "subtitles":
                           b"1\n00:00:01,000 --> 00:00:02,000\nClaim\n"}))
    register_frame_adapter("fixture.frames-v1", FrameFixture())
    config, acquired = setup_operation(tmp_path, ["subtitles", "video"])
    prepared = prepare_media_note(config, acquired["operation_id"], "chapter-1",
                                  frame_adapter="fixture.frames-v1")
    selection = tmp_path / "selection.json"; selection.write_text(json.dumps({"approved": ["frame-001"]}))
    selected = prepare_media_note(config, acquired["operation_id"], "chapter-1",
                                  frame_adapter="fixture.frames-v1", selection=selection)
    request = Path(selected["result"]["finalize_request"])

    finalized = finalize_note(config, request)
    for ref in acquired["artifact_refs"]: Path(ref).unlink()
    audited = audit_notes(config)

    assert finalized["status"] == audited["status"] == "completed"
    assert Path(finalized["result"]["note"]).is_file()
    adopted = [Path(ref) for ref in finalized["artifact_refs"] if Path(ref) != Path(finalized["result"]["note"])]
    assert len(adopted) == 1 and adopted[0].read_bytes().startswith(b"\x89PNG")


def test_public_capability_reports_asr_adapter_failure_without_claiming_completion(tmp_path: Path):
    class FailingTranscript:
        def transcribe(self, *args, **kwargs): raise RuntimeError("fixture ASR unavailable")

    register_media_adapter("fixture.media-notes-v1", MediaFixture({"audio": b"audio"}))
    register_transcript_adapter("fixture.failing-transcript-v1", FailingTranscript())
    config, acquired = setup_operation(tmp_path, ["audio"])
    request = tmp_path / "notes-request.json"
    request.write_text(json.dumps({"contract_version": 1, "workspace": str(config.config_path),
                                   "action": "prepare", "media_operation_id": acquired["operation_id"],
                                   "item_id": "chapter-1", "transcript_adapter": "fixture.failing-transcript-v1"}))

    result = run_capability("source.notes", request)

    assert result["status"] == "recoverable_failure"
    assert result["result"]["validation"]["transcript"] == "failed"
    assert result["result"]["diagnostics"] == ["transcript adapter failed; retry is available"]


def test_dot_and_spaced_srt_cues_prepare_and_finalize_without_rewriting_source(tmp_path):
    original = b"1\n00:00:01.000   -->   00:00:02.000\nClaim\n"
    register_media_adapter("fixture.media-notes-v1", MediaFixture({"video": b"video", "subtitles": original}))
    register_frame_adapter("fixture.frames-v1", FrameFixture())
    config, acquired = setup_operation(tmp_path, ["subtitles", "video"])
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"approved": ["frame-001"]}))
    selected = prepare_media_note(config, acquired["operation_id"], "chapter-1",
                                  frame_adapter="fixture.frames-v1", selection=selection)
    assert finalize_note(config, Path(selected["result"]["finalize_request"]))["status"] == "completed"
    assert any(Path(ref).read_bytes() == original for ref in acquired["artifact_refs"])


def test_frame_retry_keeps_successful_first_frame(tmp_path):
    class InterruptedFrames(FrameFixture):
        failed = False

        def extract(self, video, target, *, timestamp_ms, idempotency_token):
            if timestamp_ms == 3000 and not self.failed:
                self.failed = True
                raise RuntimeError("interrupted")
            return super().extract(video, target, timestamp_ms=timestamp_ms, idempotency_token=idempotency_token)

    subtitles = b"1\n00:00:01,000 --> 00:00:02,000\nFirst\n\n2\n00:00:03,000 --> 00:00:04,000\nSecond\n"
    register_media_adapter("fixture.media-notes-v1", MediaFixture({"video": b"video", "subtitles": subtitles}))
    frames = InterruptedFrames()
    register_frame_adapter("fixture.retry-v1", frames)
    config, acquired = setup_operation(tmp_path, ["subtitles", "video"])
    assert prepare_media_note(config, acquired["operation_id"], "chapter-1", frame_adapter="fixture.retry-v1")["status"] == "recoverable_failure"
    assert prepare_media_note(config, acquired["operation_id"], "chapter-1", frame_adapter="fixture.retry-v1")["status"] == "awaiting_model"
    assert [timestamp for _, timestamp in frames.calls] == [1000, 3000]


def test_adapter_details_and_failure_messages_do_not_leak_secrets(tmp_path):
    secret = "https://user:password@example.com?token=SECRET"

    class SecretTranscript(TranscriptFixture):
        def transcribe(self, *args, **kwargs):
            super().transcribe(*args, **kwargs)
            return {"engine": secret, "token": secret, "nested": {"secret": secret}}

    class SecretFrame:
        def extract(self, *args, **kwargs):
            raise RuntimeError(secret)

    register_media_adapter("fixture.media-notes-v1", MediaFixture({"video": b"video"}))
    register_transcript_adapter("fixture.secret-v1", SecretTranscript())
    register_frame_adapter("fixture.secret-v1", SecretFrame())
    config, acquired = setup_operation(tmp_path, ["video"])
    result = prepare_media_note(config, acquired["operation_id"], "chapter-1",
                                transcript_adapter="fixture.secret-v1", frame_adapter="fixture.secret-v1")
    assert result["status"] == "recoverable_failure"
    assert secret not in json.dumps(result)
    assert all(secret not in path.read_text() for path in config.local.rglob("*.json"))


def test_changing_adapter_identity_invalidates_transcript_and_frame_cache(tmp_path):
    register_media_adapter("fixture.media-notes-v1", MediaFixture({"video": b"video"}))
    config, acquired = setup_operation(tmp_path, ["video"])
    for version in ("v1", "v2"):
        transcript, frames = TranscriptFixture(), FrameFixture()
        register_transcript_adapter("fixture.cache-" + version, transcript)
        register_frame_adapter("fixture.cache-" + version, frames)
        result = prepare_media_note(config, acquired["operation_id"], "chapter-1",
                                    transcript_adapter="fixture.cache-" + version,
                                    frame_adapter="fixture.cache-" + version)
        assert result["status"] == "awaiting_model"
        assert transcript.calls == 1
        assert len(frames.calls) == 1


@pytest.mark.parametrize("tamper", ["omit", "replace", "review_mode", "remove_binding"])
def test_finalize_rechecks_prepared_visual_approval(tmp_path, tamper):
    register_media_adapter("fixture.media-notes-v1", MediaFixture({"video": b"video", "subtitles":
                          b"1\n00:00:01,000 --> 00:00:02,000\nClaim\n"}))
    register_frame_adapter("fixture.frames-v1", FrameFixture())
    config, acquired = setup_operation(tmp_path, ["subtitles", "video"])
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"approved": ["frame-001"]}))
    selected = prepare_media_note(config, acquired["operation_id"], "chapter-1",
                                  frame_adapter="fixture.frames-v1", selection=selection)
    request_path = Path(selected["result"]["finalize_request"])
    request = json.loads(request_path.read_text())
    if tamper == "omit":
        request["attachments"] = []
        request["markdown"] = "# Notes without approved image"
    elif tamper == "replace":
        Path(request["attachments"][0]).write_bytes(b"different image")
    elif tamper == "review_mode":
        request.setdefault("visual_review", {})["review_mode"] = "human"
    else:
        request.pop("visual_review", None)
    request_path.write_text(json.dumps(request))
    with pytest.raises(ValueError, match="visual approval"):
        finalize_note(config, request_path)


def test_explicit_reselection_records_new_decision_and_publishes_review_provenance(tmp_path):
    register_media_adapter("fixture.media-notes-v1", MediaFixture({"video": b"video", "subtitles":
                          b"1\n00:00:01,000 --> 00:00:02,000\nClaim\n"}))
    register_frame_adapter("fixture.frames-v1", FrameFixture())
    config, acquired = setup_operation(tmp_path, ["subtitles", "video"])
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"approved": ["frame-001"]}))
    first = prepare_media_note(config, acquired["operation_id"], "chapter-1",
                               frame_adapter="fixture.frames-v1", selection=selection)
    original = json.loads(Path(first["result"]["finalize_request"]).read_text())["visual_review"]
    selection.write_text(json.dumps({"approved": [], "no_useful_visuals_reason": "frame is irrelevant to this claim"}))
    selected = prepare_media_note(config, acquired["operation_id"], "chapter-1",
                                  frame_adapter="fixture.frames-v1", selection=selection, human_reviewed=True)
    result = finalize_note(config, Path(selected["result"]["finalize_request"]))
    review = result["provenance"]["visual_review"]
    assert review["review_mode"] == "human"
    assert review["approval_id"] != original["approval_id"]
    assert review["approved"] == []
    assert review["no_useful_visuals_reason"] == "frame is irrelevant to this claim"
    assert audit_notes(config)["status"] == "completed"


def test_visual_preparation_refuses_symlink_outside_results(tmp_path):
    from video_extract.workspace import WorkspaceError
    register_media_adapter("fixture.media-notes-v1", MediaFixture({"video": b"video", "subtitles":
                          b"1\n00:00:01,000 --> 00:00:02,000\nClaim\n"}))
    register_frame_adapter("fixture.frames-v1", FrameFixture())
    config, acquired = setup_operation(tmp_path, ["subtitles", "video"])
    external = tmp_path / "external"
    external.mkdir()
    notes = config.results / "source-notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "visual-preparations").symlink_to(external, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="symbolic link"):
        prepare_media_note(config, acquired["operation_id"], "chapter-1", frame_adapter="fixture.frames-v1")
    assert list(external.iterdir()) == []


def test_published_approval_remains_required_after_preparation_files_are_lost(tmp_path):
    register_media_adapter("fixture.media-notes-v1", MediaFixture({"video": b"video", "subtitles":
                          b"1\n00:00:01,000 --> 00:00:02,000\nClaim\n"}))
    register_frame_adapter("fixture.frames-v1", FrameFixture())
    config, acquired = setup_operation(tmp_path, ["subtitles", "video"])
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"approved": ["frame-001"]}))
    selected = prepare_media_note(config, acquired["operation_id"], "chapter-1",
                                  frame_adapter="fixture.frames-v1", selection=selection)
    request_path = Path(selected["result"]["finalize_request"])
    assert finalize_note(config, request_path)["status"] == "completed"
    for path in (config.results / "source-notes" / "visual-preparations").glob("*.json"):
        path.unlink()
    request = json.loads(request_path.read_text())
    request.update(expected_revision=1, markdown="# Changed without visual evidence", attachments=[])
    request.pop("visual_review")
    request_path.write_text(json.dumps(request))
    with pytest.raises(ValueError, match="visual approval"):
        finalize_note(config, request_path)
