"""Authoritative artifact-derived validation for item and collection gates."""

from __future__ import annotations

import json
import hashlib
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .contracts import DEFAULT_ARTIFACTS, GOALS, ITEM_GATES, NEXT_ITEM_GATE
from .manifest import package_path, read_json

SRT_TIME = re.compile(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}")
MARKDOWN_LINK = re.compile(r"!?\[[^]]*]\(([^)]+)\)")
TIMESTAMP = re.compile(r"\d{2}:\d{2}(?::\d{2})?(?:[,.]\d{3})?")
KNOWLEDGE_BLOCK = re.compile(r"(?m)^#{2,4}\s+(?:知识块\s*)?(\d+)[.、:]?\s*(.+)$")


@dataclass
class ValidationResult:
    kind: str
    package: str
    schema_version: int | None
    observed_gate: str | None
    passed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    next_gate: str | None = None
    counts: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load(path: Path, invalid: list[str], label: str) -> dict[str, Any] | None:
    try:
        return read_json(path)
    except Exception as exc:
        invalid.append(f"{label}: {exc}")
        return None


def _artifact(package: Path, manifest: dict[str, Any], name: str) -> Path | None:
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    raw = artifacts.get(name) or manifest.get({"transcript_srt": "subtitle"}.get(name, name)) or DEFAULT_ARTIFACTS.get(name)
    try:
        return package_path(package, raw)
    except ValueError:
        return None


def _file(path: Path | None) -> bool:
    return bool(path and path.is_file() and path.stat().st_size > 0)


def _image(path: Path | None) -> bool:
    if not _file(path):
        return False
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:
        return False


def _video(path: Path | None) -> bool:
    return _media(path, "v")


def _media(path: Path | None, stream: str | None = None) -> bool:
    if not _file(path): return False
    try:
        command = ["ffprobe", "-v", "error"]
        if stream: command += ["-select_streams", f"{stream}:0"]
        command += ["-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode: return False
        data = json.loads(result.stdout); duration = float(data.get("format", {}).get("duration") or 0)
        return duration > 0 and (not stream or any(x.get("codec_type") == {"a": "audio", "v": "video"}[stream] for x in data.get("streams", [])))
    except Exception: return False
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=30)
        return result.returncode == 0 and float(result.stdout.strip()) > 0
    except Exception:
        return False


def _audio_info(path: Path) -> dict[str, Any]:
    result = subprocess.run(["ffprobe","-v","error","-select_streams","a:0",
        "-show_entries","format=duration,bit_rate:stream=codec_name,sample_rate,bit_rate",
        "-of","json",str(path)],capture_output=True,text=True,timeout=30)
    if result.returncode: raise ValueError("ffprobe failed")
    raw=json.loads(result.stdout); stream=(raw.get("streams") or [{}])[0]; fmt=raw.get("format",{})
    return {"duration":float(fmt.get("duration") or 0),"codec":stream.get("codec_name"),
            "sample_rate":int(stream.get("sample_rate") or 0),
            "bitrate":int(stream.get("bit_rate") or fmt.get("bit_rate") or 0)}


def _resolve_markdown_links(markdown: Path, package: Path) -> list[str]:
    bad = []
    for raw in MARKDOWN_LINK.findall(markdown.read_text(encoding="utf-8")):
        target = raw.split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            if "?" in target and target.startswith(("http://", "https://")):
                bad.append(f"temporary/signed URL in {markdown.name}")
            continue
        try:
            resolved = (markdown.parent / target).resolve()
            if package.resolve() not in resolved.parents and resolved != package.resolve():
                bad.append(f"link escapes package: {raw}")
            elif not resolved.exists():
                bad.append(f"broken link: {raw}")
        except OSError:
            bad.append(f"invalid link: {raw}")
    return bad


def validate_item(package: Path) -> ValidationResult:
    package = package.expanduser().resolve()
    manifest_path = package / "manifest.json"
    invalid: list[str] = []
    if not manifest_path.exists():
        return ValidationResult("item", str(package), None, None, missing=["manifest.json"], next_gate="media_ready")
    manifest = _load(manifest_path, invalid, "manifest") or {}
    result = ValidationResult("item", str(package), manifest.get("schema_version"), None, invalid=invalid)
    video, metadata = _artifact(package, manifest, "video"), _artifact(package, manifest, "metadata")
    platform = manifest.get("platform")
    identity = manifest.get("identity") or manifest.get("source_url") or manifest.get("video")
    if not _video(video): result.missing.append("readable non-empty video with duration > 0")
    if not _file(metadata): result.missing.append("metadata")
    if not platform: result.missing.append("platform")
    if not identity: result.missing.append("stable identity")
    if result.missing or result.invalid:
        result.next_gate = "media_ready"; return result
    result.passed.append("media_ready"); result.observed_gate = "media_ready"

    transcript = _artifact(package, manifest, "transcript_srt")
    if not _file(transcript): result.missing.append("transcript SRT")
    else:
        text = transcript.read_text(encoding="utf-8-sig")
        if not SRT_TIME.search(text) or not re.search(r"-->[^\n]*\n\s*\S+", text): result.invalid.append("transcript SRT has no valid cue")
    if result.missing or result.invalid: result.next_gate = "transcript_ready"; return result
    result.passed.append("transcript_ready"); result.observed_gate = "transcript_ready"

    candidates_path, sheet = _artifact(package, manifest, "candidate_json"), _artifact(package, manifest, "contact_sheet")
    candidates = _load(candidates_path, result.invalid, "candidate JSON") if _file(candidates_path) else None
    if candidates is None: result.missing.append("candidate JSON")
    if not _image(sheet): result.missing.append("readable contact sheet")
    candidate_items = (candidates or {}).get("candidates")
    if not isinstance(candidate_items, list) or not candidate_items: result.invalid.append("candidate JSON has no candidates")
    else:
        seen = set()
        for entry in candidate_items:
            cid, ts = entry.get("id"), entry.get("timestamp")
            image = package_path(package, entry.get("image")) if entry.get("image") else None
            if not cid or cid in seen or not isinstance(ts, (int, float)) or ts < 0 or not _image(image): result.invalid.append(f"invalid candidate: {cid or '?'}")
            seen.add(cid)
    if result.missing or result.invalid: result.next_gate = "candidates_ready"; return result
    result.passed.append("candidates_ready"); result.observed_gate = "candidates_ready"

    approved_path, notes_input = _artifact(package, manifest, "approved_json"), _artifact(package, manifest, "notes_input")
    approved = _load(approved_path, result.invalid, "approved JSON") if _file(approved_path) else None
    if approved is None: result.missing.append("approved evidence JSON")
    if not _file(notes_input): result.missing.append("notes_input.md")
    if approved:
        if approved.get("review_mode") not in {"model_only", "human"}: result.invalid.append("review_mode must be model_only or human")
        if not approved.get("approved_by"): result.invalid.append("approved_by is required")
        selected = approved.get("approved")
        if not isinstance(selected, list): result.invalid.append("approved must be a list")
        elif not selected and not (approved.get("no_useful_visuals_reason") or approved.get("reason")): result.invalid.append("zero selected images requires no-useful-visuals reason")
        else:
            by_id = {entry.get("id"): entry for entry in candidate_items}
            for entry in selected:
                source = by_id.get(entry.get("id")); image = package_path(package, entry.get("image")) if entry.get("image") else None
                if not source or abs(float(entry.get("timestamp", -999)) - float(source.get("timestamp", 999))) > .01 or not _image(image): result.invalid.append(f"approved evidence cannot resolve candidate/image: {entry.get('id', '?')}")
    if result.missing or result.invalid: result.next_gate = "evidence_selected"; return result
    result.passed.append("evidence_selected"); result.observed_gate = "evidence_selected"

    notes = _artifact(package, manifest, "notes")
    if not _file(notes): result.missing.append("notes.md")
    else:
        note_text = notes.read_text(encoding="utf-8")
        for heading in ("核心知识点", "关键证据", "待核对"):
            if heading not in note_text: result.invalid.append(f"notes missing semantic section: {heading}")
        if not TIMESTAMP.search(note_text): result.invalid.append("notes contain no timestamp evidence")
        result.invalid.extend(_resolve_markdown_links(notes, package))
        input_text = notes_input.read_text(encoding="utf-8") if notes_input else ""
        for number, title in KNOWLEDGE_BLOCK.findall(input_text):
            if title.strip() not in note_text and not re.search(rf"(?:排除|省略).{{0,80}}(?:知识块\s*)?{number}\b", note_text): result.invalid.append(f"knowledge block {number} not covered or excluded")
        selected_images = {Path(x.get("image", "")).name for x in (approved or {}).get("approved", [])}
        for image_name in selected_images:
            if image_name and image_name not in note_text: result.invalid.append(f"approved image absent from notes: {image_name}")
    if result.missing or result.invalid: result.next_gate = "notes_complete"; return result
    result.passed.append("notes_complete"); result.observed_gate = "notes_complete"; result.next_gate = None
    return result


def validate_collection(root: Path, catalog_path: Path | None = None) -> ValidationResult:
    root = root.expanduser().resolve(); invalid: list[str] = []; missing: list[str] = []
    choices = [catalog_path] if catalog_path else [root / "collection.json", root / "course_catalog.json", root / "catalog.json"]
    catalog_file = next((p for p in choices if p and p.exists()), None)
    if not catalog_file: return ValidationResult("collection", str(root), None, None, missing=["catalog"], next_gate="catalog_ready")
    catalog = _load(catalog_file, invalid, "catalog") or {}
    items = catalog.get("items") if isinstance(catalog.get("items"), list) else catalog.get("lessons")
    if not isinstance(items, list) or not items: invalid.append("catalog has no complete item inventory"); items = []
    ids = set()
    for index, item in enumerate(items, 1):
        stable = item.get("id") or item.get("video_id") or item.get("cid") or item.get("url")
        order = item.get("index", item.get("sort_value"))
        if not stable or stable in ids: invalid.append(f"catalog item {index} lacks unique stable ID")
        if order is None: invalid.append(f"catalog item {index} lacks order")
        ids.add(stable)
    declared_total = catalog.get("total", catalog.get("lesson_count", len(items)))
    if declared_total != len(items): invalid.append(f"catalog denominator mismatch: declared {declared_total}, observed {len(items)}")
    gate = None if invalid else "catalog_ready"
    counts = {"total": len(items), "complete": 0, "failed_or_partial": 0, "not_started": 0}
    for item in items:
        directory = item.get("directory")
        package = (root.parent / directory if directory else root / str(item.get("title", ""))).resolve()
        if not package.exists(): counts["not_started"] += 1
        elif (package / "manifest.json").exists() and validate_item(package).observed_gate == "notes_complete": counts["complete"] += 1
        else: counts["failed_or_partial"] += 1
    result = ValidationResult("collection", str(root), catalog.get("schema_version"), gate, [gate] if gate else [], missing, invalid, None if gate else "catalog_ready", counts)
    summary_manifest = root / "summary_manifest.json"
    if gate and summary_manifest.exists():
        summary = _load(summary_manifest, result.invalid, "summary manifest") or {}
        intended = summary.get("intended_item_ids")
        summary_file = package_path(root, summary.get("summary")) if summary.get("summary") else None
        if intended == [item.get("id") or item.get("video_id") or item.get("cid") or item.get("url") for item in items] and _file(summary_file) and not _resolve_markdown_links(summary_file, root):
            result.observed_gate = "summary_complete"; result.passed.append("summary_complete")
    return result


def validate(path: Path, catalog: Path | None = None) -> ValidationResult:
    path = path.expanduser().resolve()
    if path.is_file() and path.suffix.lower() == ".json": return validate_collection(path.parent, path)
    if (path / "manifest.json").exists(): return validate_item(path)
    return validate_collection(path, catalog)


def _localized_script(package: Path, manifest: dict[str, Any]) -> list[str]:
    from .podcast import validate_localized_script
    script = _artifact(package, manifest, "localized_script"); batches = _artifact(package, manifest, "translation_batches")
    if not _file(script) or not _file(batches): return ["localized script and translation batches are required for synthesized audio"]
    try: return validate_localized_script(read_json(script), read_json(batches))
    except Exception as exc: return [f"localized script: {exc}"]


def _notes_goal(package: Path, manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    transcript = _artifact(package, manifest, "transcript_srt")
    if not _file(transcript): errors.append("transcript SRT is required")
    elif not SRT_TIME.search(transcript.read_text(encoding="utf-8-sig")): errors.append("transcript SRT has no valid timed cue")
    approved_path = _artifact(package, manifest, "approved_json"); approved = None
    if not _file(approved_path): errors.append("approved visual evidence is required")
    else:
        try: approved = read_json(approved_path)
        except Exception as exc: errors.append(f"approved evidence: {exc}")
    if approved:
        if approved.get("review_mode") not in {"model_only", "human"}: errors.append("review_mode must be model_only or human")
        selected = approved.get("approved")
        if not isinstance(selected, list): errors.append("approved evidence must be a list")
        elif not selected and not (approved.get("no_useful_visuals_reason") or approved.get("reason")): errors.append("zero selected images requires no-useful-visuals reason")
        for entry in selected or []:
            try: image = package_path(package, entry.get("image"))
            except ValueError: image = None
            if not _image(image): errors.append(f"approved image is not resolvable: {entry.get('id', '?')}")
    notes = _artifact(package, manifest, "notes")
    if not _file(notes): errors.append("Chinese notes are required")
    else:
        text = notes.read_text(encoding="utf-8")
        if not re.search(r"[\u3400-\u9fff]", text): errors.append("notes contain no Chinese text")
        if not TIMESTAMP.search(text): errors.append("notes contain no timestamp evidence")
        errors.extend(_resolve_markdown_links(notes, package))
        for entry in (approved or {}).get("approved", []):
            name = Path(entry.get("image", "")).name
            if name and name not in text: errors.append(f"approved image absent from notes: {name}")
    return errors


def validate_goals(package: Path, goals: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    """Validate final goals from artifacts, independent of historical gate labels."""
    package = package.expanduser().resolve(); path = package / "manifest.json"; errors: list[str] = []
    if not path.exists(): return {"ok": False, "package": str(package), "goals": {}, "errors": ["manifest.json is required"]}
    try: manifest = read_json(path)
    except Exception as exc: return {"ok": False, "package": str(package), "goals": {}, "errors": [f"manifest: {exc}"]}
    requested = list(goals or manifest.get("request", {}).get("goals") or [])
    unknown = [x for x in requested if x not in GOALS]
    if not requested: errors.append("no goals requested")
    if unknown: errors.append("unknown goals: " + ", ".join(unknown))
    results: dict[str, Any] = {}
    for goal in requested:
        goal_errors: list[str] = []
        if goal == "podcast_zh":
            audio = _artifact(package, manifest, "localized_audio")
            if not _media(audio, "a"): goal_errors.append("localized Mandarin audio must be ffprobe-readable with positive duration")
            provenance = manifest.get("provenance", {}).get("localized_audio")
            if not isinstance(provenance, dict) or provenance.get("kind") not in {"native_chinese_track", "synthesized"}: goal_errors.append("localized audio provenance is required")
            elif provenance.get("kind") == "synthesized":
                goal_errors.extend(_localized_script(package, manifest))
                represented = [str(x) for x in provenance.get("segments", [])]
                script_path = _artifact(package, manifest, "localized_script")
                if _file(script_path) and represented != [str(x.get("id")) for x in read_json(script_path).get("segments", [])]: goal_errors.append("localized audio does not represent all script segments")
                if audio and any(audio.parent.glob("*.partial*")): goal_errors.append("partial audio files remain")
        elif goal == "notes_zh": goal_errors.extend(_notes_goal(package, manifest))
        results[goal] = {"ok": not goal_errors, "errors": goal_errors}
    return {"ok": not errors and bool(results) and all(x["ok"] for x in results.values()), "package": str(package), "schema_version": manifest.get("schema_version"), "goals": results, "errors": errors}


def validate_media_request(package: Path) -> dict[str, Any]:
    package = package.expanduser().resolve(); errors = []
    try: manifest = read_json(package / "manifest.json")
    except Exception as exc: return {"ok": False, "package": str(package), "errors": [f"manifest: {exc}"]}
    request = manifest.get("request", {}); kinds = request.get("kinds", []); language = request.get("language", "original")
    if manifest.get("schema_version") != 5: errors.append("media request requires schema v5")
    for kind, artifact, stream in (("video", "source_video", "v"), ("audio", "source_audio", "a")):
        if kind in kinds and not (kind == "audio" and str(language).startswith("zh")):
            if not _media(_artifact(package, manifest, artifact), stream): errors.append(f"{kind} artifact is not ffprobe-readable")
    if "subtitles" in kinds and not manifest.get("results", {}).get("subtitles", {}).get("available") is False:
        path = _artifact(package, manifest, "source_subtitle")
        if not _file(path) or not SRT_TIME.search(path.read_text(encoding="utf-8-sig")): errors.append("subtitle has no valid timed cue")
    if "audio" in kinds and str(language).startswith("zh"):
        audio = _artifact(package, manifest, "localized_audio"); provenance = manifest.get("provenance", {}).get("localized_audio", {})
        if not _media(audio, "a"): errors.append("Mandarin audio is not ffprobe-readable")
        if provenance.get("kind") not in {"native_chinese_track", "synthesized"}: errors.append("Mandarin audio provenance is required")
        elif provenance.get("kind") == "synthesized":
            for artifact in ("localization_request", "localized_script", "localization_report"):
                if not _file(_artifact(package, manifest, artifact)): errors.append(f"synthesized Mandarin audio requires {artifact}")
            if not provenance.get("request_id"): errors.append("synthesized Mandarin audio requires request_id provenance")
    return {"ok": not errors, "package": str(package), "schema_version": manifest.get("schema_version"), "request": request, "errors": errors}


def validate_media_package_state(package: Path) -> dict[str, Any]:
    """Validate either a completed v5 request or a structurally valid explicit pause."""
    package = package.expanduser().resolve()
    try: manifest = read_json(package / "manifest.json")
    except Exception as exc: return {"ok": False, "package": str(package), "errors": [f"manifest: {exc}"]}
    pause = manifest.get("pause")
    if not isinstance(pause, dict):
        result = validate_media_request(package); result.update(state="complete", complete=result["ok"]); return result
    errors: list[str] = []
    request = manifest.get("request", {})
    if manifest.get("schema_version") != 5 or request.get("type") != "media": errors.append("paused media package requires schema-v5 media request")
    if pause.get("status") == "awaiting_localization":
        if pause.get("action") != "localize-audio": errors.append("awaiting_localization requires localize-audio action")
        input_raw, output_raw = pause.get("input"), pause.get("output")
        if pause.get("request"):
            try:
                request_path = package_path(package, pause["request"]); localization = read_json(request_path)
                input_raw = localization.get("source", {}).get("audio"); output_raw = localization.get("target", {}).get("audio")
                expected_hash = localization.get("source", {}).get("audio_sha256")
            except Exception as exc:
                localization = {}; expected_hash = None; errors.append(f"localization request: {exc}")
        else: expected_hash = None
        try: input_path = package_path(package, input_raw)
        except ValueError: input_path = None
        try: package_path(package, output_raw)
        except ValueError: errors.append("localization output must be package-relative")
        if not isinstance(input_raw, str) or not _media(input_path, "a"): errors.append("localization input audio is not ffprobe-readable")
        elif expected_hash and hashlib.sha256(input_path.read_bytes()).hexdigest() != expected_hash: errors.append("localization input audio hash mismatch")
        if "audio" not in request.get("kinds", []) or not str(request.get("language", "")).startswith("zh"): errors.append("awaiting_localization requires a Mandarin audio request")
        if manifest.get("artifacts", {}).get("localized_audio") or manifest.get("provenance", {}).get("localized_audio"):
            errors.append("paused package must not claim completed localized audio")
    else:
        errors.append(f"unsupported media pause status: {pause.get('status')}")
    return {"ok": not errors, "complete": False, "state": pause.get("status"), "package": str(package), "schema_version": manifest.get("schema_version"), "request": request, "pause": pause, "errors": errors}

