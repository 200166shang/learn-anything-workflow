"""Goal-driven execution with deterministic stages and explicit AI pauses."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .contracts import DEFAULT_ARTIFACTS, SCHEMA_VERSION
from .goals import AudioQuality, Goal, GoalRequest, KeepVideo
from .manifest import atomic_write_json, fingerprint, package_path, read_json, sanitize
from .media import MediaInventory, MediaMaterializer, SubtitleTrack, is_native_chinese, resolve_inventory, resolve_inventory_for_ensure, select_audio_stream
from .planner import build_plan
from .podcast import parse_srt, prepare_translation_batches, render_podcast
from .tts import MacOSSaySynthesizer, SpeechSynthesizer
from .validate import validate_goals

PROJECT = Path(__file__).resolve().parent.parent
CHINESE = re.compile(r"[\u3400-\u9fff]")


def _media_ok(path: Path | None, stream: str) -> bool:
    if not path or not path.is_file() or path.stat().st_size <= 0:
        return False
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", f"{stream}:0", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode:
        return False
    try:
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration") or 0) > 0 and bool(data.get("streams"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _resolved_artifact(package: Path, manifest: dict[str, Any], name: str) -> Path | None:
    raw = manifest.get("artifacts", {}).get(name)
    if not raw:
        return None
    try:
        return package_path(package, raw)
    except ValueError:
        return None


def _json_ok(path: Path | None, key: str) -> bool:
    if not path or not path.is_file() or not path.stat().st_size:
        return False
    try:
        return isinstance(read_json(path).get(key), list)
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _image_ok(path: Path | None) -> bool:
    if not path or not path.is_file() or not path.stat().st_size:
        return False
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        return True
    except (OSError, ValueError):
        return False


def _candidate_artifacts_ok(package: Path, manifest: dict[str, Any]) -> bool:
    path = _resolved_artifact(package, manifest, "candidate_json")
    sheet = _resolved_artifact(package, manifest, "contact_sheet")
    if not _json_ok(path, "candidates") or not _image_ok(sheet):
        return False
    candidates = read_json(path)["candidates"]
    if not candidates:
        return False
    try:
        return all(_image_ok(package_path(package, item.get("image"))) for item in candidates)
    except (TypeError, ValueError):
        return False


def _approved_artifact_ok(package: Path, manifest: dict[str, Any]) -> bool:
    path = _resolved_artifact(package, manifest, "approved_json")
    if not path or not _approved_ready(path):
        return False
    try:
        return all(_image_ok(package_path(package, item.get("image"))) for item in read_json(path)["approved"])
    except (TypeError, ValueError):
        return False


def existing_capabilities(package: Path) -> set[str]:
    """Discover reusable capabilities only from safe, minimally validated artifacts."""
    package = package.expanduser().resolve(); manifest_path = package / "manifest.json"
    if not manifest_path.is_file():
        return set()
    try:
        manifest = read_json(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return set()
    found: set[str] = set()
    checks = {
        "source_audio": lambda p: _media_ok(p, "a"),
        "source_video": lambda p: _media_ok(p, "v"),
        "video": lambda p: _media_ok(p, "v"),
        "transcript_srt": lambda p: bool(p and p.is_file() and parse_srt(p)),
        "candidate_json": lambda p: _candidate_artifacts_ok(package, manifest),
        "approved_json": lambda p: _approved_artifact_ok(package, manifest),
        "localized_script": lambda p: _json_ok(p, "segments"),
        "localized_audio": lambda p: _media_ok(p, "a"),
        "notes": lambda p: bool(p and p.is_file() and CHINESE.search(p.read_text(encoding="utf-8"))),
    }
    capabilities = {
        "source_audio": "source_audio_ready", "source_video": "source_video_ready", "video": "source_video_ready",
        "transcript_srt": "transcript_ready", "candidate_json": "candidates_ready", "approved_json": "evidence_selected",
        "localized_script": "localized_script_ready", "localized_audio": "localized_audio_ready", "notes": "notes_complete",
    }
    for artifact, check in checks.items():
        path = _resolved_artifact(package, manifest, artifact)
        try:
            valid = check(path)
        except (OSError, ValueError):
            valid = False
        if valid:
            found.update((artifact, capabilities[artifact]))
    if not validate_goals(package, ["podcast_zh"])["ok"]:
        found.difference_update({"localized_audio", "localized_audio_ready"})
    if not validate_goals(package, ["notes_zh"])["ok"]:
        found.difference_update({"notes", "notes_complete"})
    return found


def _default_transcriber(audio: Path, transcript: Path) -> dict[str, Any]:
    from convert_voice_to_article import format_timestamp, transcribe

    rows, language = transcribe(audio, transcript.parent, "small", None, "transcribe")
    blocks = [f"{index}\n{format_timestamp(row['start'])} --> {format_timestamp(row['end'])}\n{row['text']}\n" for index, row in enumerate(rows, 1)]
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("\n".join(blocks), encoding="utf-8")
    return {"engine": "faster-whisper", "model": "small", "language": language}


def _approved_ready(path: Path) -> bool:
    if not _json_ok(path, "approved"):
        return False
    data = read_json(path)
    return data.get("review_mode") in {"model_only", "human"} and bool(data.get("approved_by")) and (bool(data["approved"]) or bool(data.get("no_useful_visuals_reason") or data.get("reason")))


def _default_evidence_preparer(package: Path, video: Path, transcript: Path, approved: Path | None) -> None:
    """Run/reuse the existing deterministic candidate generator and refresh notes input."""
    candidate_path = package / DEFAULT_ARTIFACTS["candidate_json"]
    sheet = package / DEFAULT_ARTIFACTS["contact_sheet"]
    notes_input = package / DEFAULT_ARTIFACTS["notes_input"]
    if not (_json_ok(candidate_path, "candidates") and sheet.is_file() and notes_input.is_file()):
        completed = subprocess.run(
            [sys.executable, str(PROJECT / "prepare_learning_package.py"), str(video), "--transcript", str(transcript), "--output-dir", str(package), "--no-ocr"],
            capture_output=True, text=True,
        )
        if completed.returncode:
            raise RuntimeError((completed.stderr or completed.stdout).strip() or "candidate preparation failed")
        pending = package / DEFAULT_ARTIFACTS["approved_json"]
        if pending.exists() and not _approved_ready(pending):
            pending.unlink()
    if approved and _approved_ready(approved):
        from prepare_learning_package import Candidate, write_ai_input

        raw_candidates = read_json(candidate_path).get("candidates", [])
        selected = {str(item.get("id")): item for item in read_json(approved).get("approved", [])}
        candidates = []
        for raw in raw_candidates:
            fields = {key: raw[key] for key in Candidate.__dataclass_fields__ if key in raw}
            candidate = Candidate(**fields)
            if candidate.id in selected:
                choice = selected[candidate.id]
                candidate.status = choice.get("status", "keep")
                candidate.note = choice.get("note", candidate.note)
                candidate.evidence_for = choice.get("evidence_for", candidate.evidence_for)
            else:
                candidate.status = "reject"
            candidates.append(candidate)
        write_ai_input(candidates, package, video, transcript)


@dataclass
class EnsureDependencies:
    inventory_resolver: Callable[[str], MediaInventory] = resolve_inventory_for_ensure
    materializer_factory: Callable[[str], MediaMaterializer] = lambda bitrate: MediaMaterializer(audio_bitrate=bitrate)
    transcriber: Callable[[Path, Path], dict[str, Any]] = _default_transcriber
    evidence_preparer: Callable[[Path, Path, Path, Path | None], None] = _default_evidence_preparer
    synthesizer_factory: Callable[[], SpeechSynthesizer] = MacOSSaySynthesizer


def _audio_bitrate(request: GoalRequest) -> str:
    return "128k" if request.audio_quality is AudioQuality.STANDARD else "192k"


@dataclass(frozen=True)
class LegacyAudio:
    path: Path
    relative_path: str
    language: str
    schema_version: int
    native_chinese: bool


def _legacy_audio(package: Path) -> LegacyAudio | None:
    """Find a safe, decodable schema v1-v3 `artifacts.audio` entry."""
    manifest_path = package / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = read_json(manifest_path)
        schema_version = int(manifest.get("schema_version"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if schema_version not in {1, 2, 3}:
        return None
    raw = manifest.get("artifacts", {}).get("audio") if isinstance(manifest.get("artifacts"), dict) else None
    if not isinstance(raw, str):
        return None
    try:
        path = package_path(package, raw)
    except ValueError:
        return None
    if not _media_ok(path, "a"):
        return None
    language = str(manifest.get("selected_audio_language") or "").strip()
    return LegacyAudio(path, raw, language, schema_version, is_native_chinese(language))


def _audio_codec(path: Path) -> str | None:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    return result.stdout.strip().casefold() if result.returncode == 0 else None


def _normalize_legacy_audio(source: Path, target: Path, materializer: MediaMaterializer) -> str:
    """Atomically copy suitable AAC/M4A; transcode only incompatible legacy audio."""
    if source.resolve() == target.resolve():
        return "existing"
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.casefold() == ".m4a" and _audio_codec(source) == "aac":
        fd, raw = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".partial", dir=target.parent)
        os.close(fd); temporary = Path(raw)
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return "atomic_copy"
    materializer.audio_from_local(source, target)
    return "aac_transcode"


def _manifest(package: Path, inventory: MediaInventory, request: GoalRequest, preexisting_video: bool) -> dict[str, Any]:
    path = package / "manifest.json"; data = read_json(path) if path.exists() else {}
    prior_retention = data.get("retention", {}) if isinstance(data.get("retention"), dict) else {}
    data.update({
        "schema_version": SCHEMA_VERSION, "platform": inventory.platform, "identity": inventory.identity, "title": inventory.title,
        "request": request.to_dict(), "retention": {**prior_retention, "keep_video": request.keep_video.value, "source_video_preexisted": preexisting_video},
        "artifacts": data.get("artifacts", {}), "stages": data.get("stages", {}),
    })
    return data


def _observe(data: dict[str, Any], name: str, started: float, fp: str, parameters: dict[str, Any], cache_hit: bool = False) -> None:
    data.setdefault("stages", {})[name] = sanitize({"duration_ms": round((time.monotonic() - started) * 1000), "fingerprint": fp, "parameters": parameters, "cache_hit": cache_hit, "result": "passed"})


def _pause(package: Path, data: dict[str, Any], action: str, input_path: str, output_path: str, source: str, request: GoalRequest) -> dict[str, Any]:
    resume = ["video-extract", "ensure", source]
    for goal in request.goals:
        resume += ["--goal", goal.value]
    resume += ["--keep-video", request.keep_video.value, "--audio-quality", request.audio_quality.value, "--video-quality", str(request.resolved_video_quality), "--voice", request.voice, "--output", str(package), "--json"]
    pause = {"status": "awaiting_ai", "action": action, "input": input_path, "output": output_path, "schema": {"podcast_translate": "localized-script-v1", "evidence_select": "approved-keyframes-v3", "notes_write": "notes-markdown-v1"}[action], "resume": resume}
    data["pause"] = pause; atomic_write_json(package / "manifest.json", data)
    return pause


def _choose_video(inventory: MediaInventory, max_height: int):
    eligible = [x for x in inventory.video_streams if x.url and (x.height is None or x.height <= max_height)]
    return max(eligible, key=lambda x: x.height or 0, default=None) or next((x for x in inventory.video_streams if x.url), None)


def _ensure_source_audio(package: Path, data: dict[str, Any], inventory: MediaInventory, source_path: Path | None, materializer: MediaMaterializer, quality: AudioQuality) -> Path:
    artifacts = data["artifacts"]; existing = _resolved_artifact(package, data, "source_audio")
    if _media_ok(existing, "a"):
        return existing
    target = package / DEFAULT_ARTIFACTS["source_audio"]; started = time.monotonic()
    if source_path and inventory.audio_streams:
        materializer.audio_from_local(source_path, target); stream_id = "local"
    else:
        stream = select_audio_stream(inventory, quality)
        if not stream:
            raise RuntimeError("source has no materializable audio stream")
        # For YouTube, download from the page URL so yt-dlp can resolve and
        # retry its fragments. The inventory stream URL is a short-lived
        # signed URL intended for a single direct request.
        download_source = inventory.source if inventory.platform == "youtube" and inventory.source else stream
        materializer.audio_from_url(download_source, target); stream_id = stream.id
    artifacts["source_audio"] = DEFAULT_ARTIFACTS["source_audio"]
    _observe(data, "source_audio_ready", started, fingerprint([source_path] if source_path else [], {"identity": inventory.identity, "stream": stream_id}), {"stream": stream_id})
    return target


def _ensure_transcript(package: Path, data: dict[str, Any], inventory: MediaInventory, source_path: Path | None, materializer: MediaMaterializer, deps: EnsureDependencies, quality: AudioQuality) -> Path:
    existing = _resolved_artifact(package, data, "transcript_srt")
    if existing:
        try:
            if parse_srt(existing):
                return existing
        except (OSError, ValueError):
            pass
    target = package / DEFAULT_ARTIFACTS["transcript_srt"]
    formal: SubtitleTrack | None = next((track for track in inventory.subtitles if not track.automatic and track.url), None)
    started = time.monotonic()
    if formal:
        materializer.subtitle_from_url(formal, target)
        parameters = {"source": "formal_subtitle", "language": formal.language, "track": formal.id}
    else:
        audio = _ensure_source_audio(package, data, inventory, source_path, materializer, quality)
        parameters = deps.transcriber(audio, target)
    if not target.is_file() or not parse_srt(target):
        raise RuntimeError("transcript materialization did not produce valid timed cues")
    data["artifacts"]["transcript_srt"] = DEFAULT_ARTIFACTS["transcript_srt"]
    _observe(data, "transcript_ready", started, fingerprint([target], parameters), parameters)
    return target


def _detect_preexisting_video(package: Path) -> bool:
    manifest_path = package / "manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = read_json(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    retention = manifest.get("retention", {})
    if retention.get("source_video_acquired_by_ensure"):
        return bool(retention.get("source_video_preexisted", False))
    candidates = [_resolved_artifact(package, manifest, name) for name in ("source_video", "video")]
    candidates.append(package / DEFAULT_ARTIFACTS["source_video"])
    return any(_media_ok(path, "v") for path in candidates)


def ensure(source: str, package: Path, request: GoalRequest, dependencies: EnsureDependencies | None = None) -> dict[str, Any]:
    deps = dependencies or EnsureDependencies()
    package = package.expanduser().resolve(); package.mkdir(parents=True, exist_ok=True)
    preexisting_video = _detect_preexisting_video(package)
    legacy_audio = _legacy_audio(package)
    legacy_native_only = bool(
        legacy_audio and legacy_audio.native_chinese
        and request.goals == (Goal.PODCAST_ZH,)
    )
    if legacy_native_only:
        old = read_json(package / "manifest.json")
        inventory = MediaInventory(
            str(old.get("platform") or "youtube"), old.get("identity") or source,
            str(old.get("title") or source), None, source=source,
            original_language=legacy_audio.language, default_audio_language=legacy_audio.language,
        )
    else:
        inventory = deps.inventory_resolver(source)
    data = _manifest(package, inventory, request, preexisting_video)
    bitrate = _audio_bitrate(request); materializer = deps.materializer_factory(bitrate)
    artifacts = data["artifacts"]
    source_path = Path(source).expanduser().resolve() if inventory.platform == "local" else None

    legacy_podcast_done = False
    if legacy_audio:
        artifacts["source_audio"] = legacy_audio.relative_path
        if legacy_audio.native_chinese and Goal.PODCAST_ZH in request.goals:
            target = package / DEFAULT_ARTIFACTS["localized_audio"]
            started = time.monotonic()
            mode = "cache_hit"
            if not _media_ok(target, "a"):
                mode = _normalize_legacy_audio(legacy_audio.path, target, materializer)
            if not _media_ok(target, "a"):
                raise RuntimeError("legacy audio normalization did not produce valid localized audio")
            artifacts["localized_audio"] = DEFAULT_ARTIFACTS["localized_audio"]
            data.setdefault("provenance", {})["localized_audio"] = {
                "kind": "native_chinese_track", "language": legacy_audio.language,
                "source": "legacy_manifest_audio", "source_schema_version": legacy_audio.schema_version,
                "source_artifact": legacy_audio.relative_path, "reused_legacy_audio": True,
                "normalization": mode, "audio_bitrate": bitrate,
            }
            _observe(data, "localized_audio_ready", started, fingerprint([legacy_audio.path], {"language": legacy_audio.language}), {"source": "legacy_manifest_audio", "normalization": mode}, mode == "cache_hit")
            legacy_podcast_done = True

    initial_validation = validate_goals(package, [goal.value for goal in request.goals])
    provenance = data.get("provenance", {}).get("localized_audio", {})
    podcast_done = legacy_podcast_done or (initial_validation.get("goals", {}).get("podcast_zh", {}).get("ok", False) and provenance.get("audio_bitrate") == bitrate)
    notes_done = initial_validation.get("goals", {}).get("notes_zh", {}).get("ok", False)
    if notes_done and request.keep_video is KeepVideo.YES:
        notes_done = any(_media_ok(_resolved_artifact(package, data, name), "v") for name in ("source_video", "video"))
    cleanup_pending = notes_done and request.keep_video is not KeepVideo.YES and not data["retention"]["source_video_preexisted"] and any(_media_ok(_resolved_artifact(package, data, name), "v") for name in ("source_video", "video"))
    if not cleanup_pending and (Goal.PODCAST_ZH not in request.goals or podcast_done) and (Goal.NOTES_ZH not in request.goals or notes_done):
        data.pop("pause", None); atomic_write_json(package / "manifest.json", data)
        return {"status": "complete", "package": str(package), "goals": validate_goals(package, [goal.value for goal in request.goals])}

    if source_path and inventory.video_streams and Goal.NOTES_ZH in request.goals and not notes_done:
        local_video = package / DEFAULT_ARTIFACTS["source_video"]
        if not _media_ok(local_video, "v"):
            materializer.copy_local_video(source_path, local_video)
        artifacts["source_video"] = DEFAULT_ARTIFACTS["source_video"]; artifacts["video"] = DEFAULT_ARTIFACTS["video"]
        data["retention"]["source_video_preexisted"] = True

    native = select_audio_stream(inventory, request.audio_quality, "zh")
    if Goal.PODCAST_ZH in request.goals and native and not podcast_done:
        target = package / DEFAULT_ARTIFACTS["localized_audio"]
        prior_audio = data.get("provenance", {}).get("localized_audio", {})
        if not _media_ok(target, "a") or prior_audio.get("audio_bitrate") != bitrate:
            if source_path:
                materializer.audio_from_local(source_path, target)
            elif native.url:
                download_source = inventory.source if inventory.platform == "youtube" and inventory.source else native
                materializer.audio_from_url(download_source, target)
            else:
                raise RuntimeError("selected native Chinese stream has no materializable URL")
        artifacts["localized_audio"] = DEFAULT_ARTIFACTS["localized_audio"]
        data.setdefault("provenance", {})["localized_audio"] = {"kind": "native_chinese_track", "language": native.language, "stream_id": native.id, "source_bitrate": native.bitrate, "audio_bitrate": bitrate}

    transcript: Path | None = None
    if (Goal.PODCAST_ZH in request.goals and not native and not podcast_done) or (Goal.NOTES_ZH in request.goals and not notes_done):
        transcript = _ensure_transcript(package, data, inventory, source_path, materializer, deps, request.audio_quality)

    if Goal.PODCAST_ZH in request.goals and not native and not podcast_done:
        assert transcript is not None
        batches = package / DEFAULT_ARTIFACTS["translation_batches"]
        if not batches.exists():
            prepare_translation_batches(transcript, batches)
        artifacts["translation_batches"] = DEFAULT_ARTIFACTS["translation_batches"]
        script = package / DEFAULT_ARTIFACTS["localized_script"]
        if not script.exists():
            return _pause(package, data, "podcast_translate", DEFAULT_ARTIFACTS["translation_batches"], DEFAULT_ARTIFACTS["localized_script"], source, request)
        artifacts["localized_script"] = DEFAULT_ARTIFACTS["localized_script"]
        podcast = package / DEFAULT_ARTIFACTS["localized_audio"]
        prior_audio = data.get("provenance", {}).get("localized_audio", {})
        if not _media_ok(podcast, "a") or prior_audio.get("audio_bitrate") != bitrate:
            provenance = render_podcast(script, batches, podcast, deps.synthesizer_factory(), request.voice, audio_bitrate=bitrate)
            data.setdefault("provenance", {})["localized_audio"] = provenance
        artifacts["localized_audio"] = DEFAULT_ARTIFACTS["localized_audio"]

    if Goal.NOTES_ZH in request.goals and not notes_done:
        assert transcript is not None
        video = _resolved_artifact(package, data, "source_video") or package / DEFAULT_ARTIFACTS["source_video"]
        if not _media_ok(video, "v"):
            stream = _choose_video(inventory, request.resolved_video_quality)
            if not stream:
                raise RuntimeError("source has no materializable video stream for visual evidence")
            started = time.monotonic(); materializer.video_from_url(stream, video)
            artifacts["source_video"] = DEFAULT_ARTIFACTS["source_video"]; artifacts["video"] = DEFAULT_ARTIFACTS["video"]
            data["retention"]["source_video_acquired_by_ensure"] = True
            _observe(data, "source_video_ready", started, fingerprint([], {"identity": inventory.identity, "stream": stream.id}), {"stream": stream.id, "max_height": request.resolved_video_quality})
        approved = package / DEFAULT_ARTIFACTS["approved_json"]
        deps.evidence_preparer(package, video, transcript, approved if _approved_ready(approved) else None)
        artifacts.update({"candidate_json": DEFAULT_ARTIFACTS["candidate_json"], "contact_sheet": DEFAULT_ARTIFACTS["contact_sheet"], "notes_input": DEFAULT_ARTIFACTS["notes_input"]})
        if not _approved_ready(approved):
            artifacts.pop("approved_json", None)
            return _pause(package, data, "evidence_select", DEFAULT_ARTIFACTS["candidate_json"], DEFAULT_ARTIFACTS["approved_json"], source, request)
        artifacts["approved_json"] = DEFAULT_ARTIFACTS["approved_json"]
        notes = package / DEFAULT_ARTIFACTS["notes"]
        notes_valid = False
        if notes.exists():
            artifacts["notes"] = DEFAULT_ARTIFACTS["notes"]
            atomic_write_json(package / "manifest.json", data)
            notes_valid = validate_goals(package, ["notes_zh"]).get("goals", {}).get("notes_zh", {}).get("ok", False)
        if not notes_valid:
            artifacts.pop("notes", None)
            return _pause(package, data, "notes_write", DEFAULT_ARTIFACTS["notes_input"], DEFAULT_ARTIFACTS["notes"], source, request)
        artifacts["notes"] = DEFAULT_ARTIFACTS["notes"]

    data.pop("pause", None); atomic_write_json(package / "manifest.json", data)
    validation = validate_goals(package, [goal.value for goal in request.goals])
    notes_verified = validation.get("goals", {}).get("notes_zh", {}).get("ok", False)
    if validation["ok"] and notes_verified and request.keep_video is not KeepVideo.YES and not data["retention"]["source_video_preexisted"]:
        raw = artifacts.get("source_video") or artifacts.get("video")
        try:
            video = package_path(package, raw)
        except ValueError:
            video = None
        if video and video.is_file():
            video.unlink()
        artifacts.pop("source_video", None); artifacts.pop("video", None)
        data["retention"]["proxy_removed_after_verification"] = True
        atomic_write_json(package / "manifest.json", data)
        validation = validate_goals(package, [goal.value for goal in request.goals])
    return {"status": "complete" if validation["ok"] else "failed", "package": str(package), "goals": validation}


def plan(source: str, request: GoalRequest, package: Path | None = None, inventory_resolver: Callable[[str], MediaInventory] | None = None) -> dict[str, Any]:
    inventory = (inventory_resolver or resolve_inventory)(source); existing = existing_capabilities(package) if package else set()
    return build_plan(inventory, request, existing)
