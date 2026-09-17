"""Prepare authoritative source notes from scoped media operation artifacts.

The operation is deliberately adapter-driven: production integrations may use
Whisper/ffmpeg, while tests and offline callers can provide deterministic local
adapters.  Candidate images are always decoded from the acquired video bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Protocol

from .authoritative_notes import prepare_note
from .command_response import response
from .manifest import atomic_write_json, read_json
from .source_registry import register
from .workspace import WorkspaceConfig, WorkspaceError
from .timed_cues import cue_ranges
from .visual_approval import approval_record, record_preparation


class TranscriptAdapter(Protocol):
    def transcribe(self, media: Path, target: Path, *, language: str,
                   idempotency_token: str) -> dict[str, Any]: ...


class FrameAdapter(Protocol):
    def extract(self, video: Path, target: Path, *, timestamp_ms: int,
                idempotency_token: str) -> dict[str, Any]: ...


class BuiltinTranscriptAdapter:
    def transcribe(self, media: Path, target: Path, *, language: str,
                   idempotency_token: str) -> dict[str, Any]:
        from .orchestrator import _default_transcriber
        return _default_transcriber(media, target)


class BuiltinFrameAdapter:
    def extract(self, video: Path, target: Path, *, timestamp_ms: int,
                idempotency_token: str) -> dict[str, Any]:
        target.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{timestamp_ms / 1000:.3f}",
             "-i", str(video), "-frames:v", "1", str(target)], capture_output=True, text=True
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or "ffmpeg frame extraction failed")
        return {"decoder": "ffmpeg", "timestamp_ms": timestamp_ms}


TRANSCRIPT_ADAPTERS: dict[str, TranscriptAdapter] = {"builtin.transcript-v1": BuiltinTranscriptAdapter()}
FRAME_ADAPTERS: dict[str, FrameAdapter] = {"builtin.frames-v1": BuiltinFrameAdapter()}


def register_transcript_adapter(adapter_id: str, adapter: TranscriptAdapter) -> None:
    TRANSCRIPT_ADAPTERS[adapter_id] = adapter


def register_frame_adapter(adapter_id: str, adapter: FrameAdapter) -> None:
    FRAME_ADAPTERS[adapter_id] = adapter


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _operation(config: WorkspaceConfig, operation_id: str) -> dict[str, Any]:
    if not config.local or not config.results:
        raise WorkspaceError("workspace schema v2 is required for media note preparation")
    path = config.local / "operations" / f"{operation_id}.json"
    if not path.is_file():
        raise ValueError(f"unknown media operation: {operation_id}")
    value = read_json(path)
    if value.get("status") != "completed":
        raise ValueError("media operation is not completed and verified")
    return value


def _artifact(config: WorkspaceConfig, operation: dict[str, Any], item_id: str,
              kind: str) -> Path | None:
    assert config.results is not None
    raw = operation.get("artifacts", {}).get(f"{item_id}:{kind}")
    if not raw:
        return None
    path = (config.results / raw).resolve(strict=False)
    if config.results.resolve(strict=False) not in path.parents or not path.is_file():
        return None
    fact = operation.get("artifact_facts", {}).get(f"{item_id}:{kind}", {})
    if fact.get("sha256") != _sha256(path) or fact.get("size") != path.stat().st_size:
        return None
    return path


def _cue_timestamps(path: Path) -> list[int]:
    text = path.read_text(encoding="utf-8-sig")
    found = []
    for cue in cue_ranges(text):
        hours, minutes, seconds, millis = re.split(r"[:,]", cue.split(" --> ")[0])
        found.append((((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)))
    return found[:3]


def prepare_media_note(config: WorkspaceConfig, operation_id: str, item_id: str, *,
                       transcript_adapter: str | None = None,
                       frame_adapter: str | None = None,
                       require_visuals: bool = False,
                       selection: Path | None = None,
                       human_reviewed: bool = False) -> dict[str, Any]:
    """Prepare a registered transcript and real-frame candidates for one item."""
    operation = _operation(config, operation_id)
    if item_id not in operation["request"]["scope"]:
        raise ValueError("item_id is outside the acquired operation scope")
    assert config.local is not None
    root = config.local / "media-note-operations" / operation_id / hashlib.sha256(item_id.encode()).hexdigest()[:16]
    root.mkdir(parents=True, exist_ok=True)
    subtitle = _artifact(config, operation, item_id, "subtitles")
    audio = _artifact(config, operation, item_id, "audio")
    video = _artifact(config, operation, item_id, "video")
    transcript = root / "transcript.source.srt"
    transcript_receipt = root / "transcript.json"
    if subtitle:
        if not transcript.is_file() or _sha256(transcript) != _sha256(subtitle):
            transcript.write_bytes(subtitle.read_bytes())
            atomic_write_json(transcript_receipt, {"origin": "formal_subtitles", "sha256": _sha256(transcript)})
    else:
        media = audio or video
        adapter_id = transcript_adapter or "builtin.transcript-v1"
        adapter = TRANSCRIPT_ADAPTERS.get(adapter_id)
        if not media:
            return response(status="missing_input", workspace=str(config.config_path), operation_id=operation_id,
                            result={"available": []}, validation={"transcript": "missing"},
                            next_action={"type": "input", "missing": "audio_or_video"})
        if adapter is None:
            return response(status="missing_dependency", workspace=str(config.config_path), operation_id=operation_id,
                            result={"available": [media.name]}, validation={"transcript": "not_checked"},
                            next_action={"type": "dependency", "missing": "transcript_adapter"})
        media_sha = _sha256(media)
        receipt = read_json(transcript_receipt) if transcript_receipt.is_file() else {}
        reusable = (transcript.is_file() and receipt.get("origin") == "asr"
                    and receipt.get("adapter_id") == adapter_id
                    and receipt.get("input_sha256") == media_sha
                    and receipt.get("sha256") == _sha256(transcript) and bool(_cue_timestamps(transcript)))
        if not reusable:
            token = hashlib.sha256(f"{operation_id}:{item_id}:transcript:{media_sha}:{adapter_id}".encode()).hexdigest()
            try:
                adapter.transcribe(media, transcript, language=operation["request"]["language"],
                                             idempotency_token=token)
            except Exception:
                return response(status="recoverable_failure", workspace=str(config.config_path),
                                operation_id=operation_id, result={"available": [media.name]},
                                validation={"transcript": "failed"}, diagnostics=["transcript adapter failed; retry is available"],
                                next_action={"type": "retry", "stage": "transcript"})
            if not transcript.is_file() or not _cue_timestamps(transcript):
                raise WorkspaceError("transcript adapter did not produce a valid timed transcript")
            atomic_write_json(transcript_receipt, {"origin": "asr", "input_sha256": media_sha, "adapter_id": adapter_id,
                                                    "sha256": _sha256(transcript)})
    if not _cue_timestamps(transcript):
        raise ValueError("prepared transcript has no complete timed cues")
    registered = register(config, transcript, title=f"{item_id} transcript")["result"]
    note = prepare_note(config, registered["source_id"], registered["source_version"])
    evidence: dict[str, Any] = {"kind": "audio_only", "candidates": []}
    if require_visuals and not video:
        return response(status="missing_input", workspace=str(config.config_path), operation_id=operation_id,
                        result={"available": ["transcript"], "source_id": registered["source_id"]},
                        validation={"transcript": "passed", "visual_evidence": "missing"},
                        next_action={"type": "input", "missing": "video"})
    if video:
        record_preparation(config, registered["source_id"], registered["source_version"], None)
        adapter_id = frame_adapter or "builtin.frames-v1"
        adapter = FRAME_ADAPTERS.get(adapter_id)
        if adapter is None:
            return response(status="missing_dependency", workspace=str(config.config_path), operation_id=operation_id,
                            result={"available": ["transcript", "video"], "source_id": registered["source_id"]},
                            validation={"transcript": "passed", "visual_evidence": "not_checked"},
                            next_action={"type": "dependency", "missing": "frame_adapter"})
        video_sha = _sha256(video)
        candidate_index = root / "candidates.json"
        previous_candidates = {}
        if candidate_index.is_file():
            previous_candidates = {item.get("candidate_id"): item for item in read_json(candidate_index).get("candidates", [])}
        candidates = []
        for index, timestamp_ms in enumerate(_cue_timestamps(transcript), 1):
            candidate_id = f"frame-{index:03d}"
            image = root / "candidates" / f"frame-{index:03d}-{timestamp_ms}.png"
            token = hashlib.sha256(f"{operation_id}:{item_id}:frame:{video_sha}:{timestamp_ms}:{adapter_id}".encode()).hexdigest()
            previous = previous_candidates.get(candidate_id, {})
            reusable = (image.is_file() and previous.get("timestamp_ms") == timestamp_ms
                        and previous.get("adapter_id") == adapter_id
                        and previous.get("source_video_sha256") == video_sha
                        and previous.get("image_sha256") == _sha256(image))
            if not reusable:
                try:
                    adapter.extract(video, image, timestamp_ms=timestamp_ms, idempotency_token=token)
                    details = {"result": "decoded_frame"}
                except Exception:
                    return response(status="recoverable_failure", workspace=str(config.config_path),
                                    operation_id=operation_id,
                                    result={"available": ["transcript", "video"], "candidate_id": candidate_id},
                                    validation={"transcript": "passed", "visual_evidence": "failed"},
                                    diagnostics=["frame adapter failed; retry is available"],
                                    next_action={"type": "retry", "stage": "frame", "candidate_id": candidate_id})
            else:
                details = {"reuse": "verified_candidate"}
            if not image.is_file() or image.stat().st_size == 0:
                raise WorkspaceError("frame adapter did not produce a non-empty image")
            candidates.append({"candidate_id": candidate_id, "timestamp_ms": timestamp_ms, "adapter_id": adapter_id,
                               "image": str(image), "image_sha256": _sha256(image),
                               "source_video_sha256": video_sha, "provenance": details})
            atomic_write_json(candidate_index, {"kind": "video_frames", "review_mode": "unreviewed",
                                                 "candidates": candidates})
        evidence = {"kind": "video_frames", "review_mode": "unreviewed", "candidates": candidates,
                    "candidate_index": str(candidate_index)}
        atomic_write_json(candidate_index, evidence)
        if selection is not None:
            decision = read_json(selection)
            approved_ids = decision.get("approved")
            if not isinstance(approved_ids, list) or not all(isinstance(item, str) for item in approved_ids):
                raise ValueError("selection approved must be a list of candidate IDs")
            by_id = {item["candidate_id"]: item for item in candidates}
            if len(set(approved_ids)) != len(approved_ids) or any(item not in by_id for item in approved_ids):
                raise ValueError("selection references an unknown or duplicate frame candidate")
            approved = [by_id[item] for item in approved_ids]
            evidence.update({"review_mode": "human" if human_reviewed else "model_only",
                             "approved": approved,
                             "no_useful_visuals_reason": decision.get("no_useful_visuals_reason")})
            if not approved and not evidence["no_useful_visuals_reason"]:
                raise ValueError("an empty visual selection requires no_useful_visuals_reason")
            approved_index = root / "approved.json"
            atomic_write_json(approved_index, evidence)
            evidence["approved_index"] = str(approved_index)
    result = dict(note["result"])
    result["evidence"] = evidence
    if video and evidence.get("review_mode") == "unreviewed":
        next_action = {"type": "model", "action": "select visual evidence",
                       "candidate_index": evidence["candidate_index"]}
    else:
        next_action = note["next_action"]
    if evidence.get("approved") is not None:
        visual_review = approval_record(evidence)
        record_preparation(config, registered["source_id"], registered["source_version"], visual_review)
        request_path = root / "finalize-request.json"
        image_lines = "\n".join(
            f"![画面证据 {item['candidate_id']}]({Path(item['image']).name})" for item in evidence["approved"]
        )
        template = {"schema_version": 1, "source_id": registered["source_id"],
                    "source_version": registered["source_version"], "expected_revision": 0,
                    "markdown": "# 来源笔记\n\n<!-- 模型应以转写证据完成正文，并保留所有采用画面 -->\n\n" + image_lines,
                    "citations": [{"claim": "待模型填写", "locator_type": "timestamp",
                                   "locator": cue_ranges(transcript.read_text(encoding="utf-8-sig"))[0]}],
                    "corrections": [], "attachments": [item["image"] for item in evidence["approved"]],
                    "visual_review": visual_review,
                    "association": {"status": "unverified", "evidence": None}}
        atomic_write_json(request_path, template)
        result["finalize_request"] = str(request_path)
    return response(status="awaiting_model", workspace=str(config.config_path), operation_id=operation_id,
                    result=result, validation={"transcript": "passed", "visual_evidence": "passed"},
                    provenance={"media_operation_id": operation_id, "media_source_version": operation["request"]["source_version"],
                                "transcript_source_id": registered["source_id"],
                                "transcript_source_version": registered["source_version"]},
                    next_action=next_action, artifact_refs=[result["model_input"],
                                                           *[item["image"] for item in evidence["candidates"]]])
