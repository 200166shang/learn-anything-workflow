"""Local-only preparation and finalization for source-grounded notes."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .manifest import atomic_write_json, read_json
from .package_lock import package_lock
from .podcast import parse_srt
from .validate import validate_goals
from .workspace import WorkspaceConfig


def _artifact(package: Path, data: dict[str, Any], key: str) -> Path | None:
    raw = data.get("artifacts", {}).get(key)
    if not raw:
        return None
    path = (package / raw).resolve()
    return path if package.resolve() in path.parents else None


def _pause(package: Path, data: dict[str, Any], action: str, input_path: str, output_path: str,
           workspace: WorkspaceConfig) -> dict[str, Any]:
    resume = ["video-extract", "notes", "prepare", str(package), "--workspace", str(workspace.config_path), "--json"]
    pause = {"status": "awaiting_ai", "action": action, "input": input_path, "output": output_path, "resume": resume}
    data["pause"] = pause
    atomic_write_json(package / "manifest.json", data)
    return {**pause, "package": str(package)}


def _ensure_transcript(package: Path, data: dict[str, Any]) -> Path | None:
    existing = _artifact(package, data, "transcript_srt")
    if existing and existing.is_file() and parse_srt(existing):
        return existing
    source = _artifact(package, data, "source_audio") or _artifact(package, data, "source_video")
    if not source or not source.is_file():
        return None
    audio = source
    if data.get("source_kind") == "video":
        audio = package / "media/audio.source.m4a"
        if not audio.is_file():
            audio.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-vn", "-c:a", "aac", str(audio)], check=True)
        data["artifacts"]["source_audio"] = "media/audio.source.m4a"
    target = package / "subtitles/transcript.source.srt"
    from .orchestrator import _default_transcriber
    parameters = _default_transcriber(audio, target)
    data["artifacts"]["transcript_srt"] = "subtitles/transcript.source.srt"
    data.setdefault("stages", {})["transcript_ready"] = {"parameters": parameters, "result": "passed"}
    return target


def _prepare_text_input(package: Path, data: dict[str, Any], source: Path, source_kind: str) -> None:
    notes_dir = package / "notes"; notes_dir.mkdir(parents=True, exist_ok=True)
    approved = package / "evidence/approved.json"; approved.parent.mkdir(parents=True, exist_ok=True)
    if not approved.is_file():
        atomic_write_json(approved, {"schema_version": 1, "review_mode": "model_only", "approved_by": "codex",
                                    "approved": [], "no_useful_visuals_reason": f"{source_kind} source has no video evidence"})
    data["artifacts"]["approved_json"] = "evidence/approved.json"
    notes_input = notes_dir / "notes_input.md"
    locator = "timestamp cues" if source_kind in {"audio", "transcript"} else "headings and paragraphs"
    notes_input.write_text(f"# Source notes input\n\nSource kind: {source_kind}\nUse {locator} for evidence.\n\n" + source.read_text(encoding="utf-8-sig"), encoding="utf-8")
    data["artifacts"]["notes_input"] = "notes/notes_input.md"


def _prepare_video(package: Path, data: dict[str, Any], transcript: Path) -> None:
    video = _artifact(package, data, "source_video")
    if not video or not video.is_file():
        raise ValueError("video source is missing")
    command = [sys.executable, str(Path(__file__).resolve().parent.parent / "prepare_learning_package.py"), str(video),
               "--transcript", str(transcript), "--output-dir", str(package), "--no-ocr"]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    evidence = package / "evidence"; images = evidence / "images"; images.mkdir(parents=True, exist_ok=True)
    old_images = package / "frames/keyframes"
    if old_images.is_dir():
        for image in old_images.iterdir():
            if image.is_file(): shutil.move(str(image), images / image.name)
    sheet = package / "frames/contact_sheet.jpg"
    if sheet.is_file(): shutil.move(str(sheet), evidence / "contact-sheet.jpg")
    for old, new in ((package / "review/keyframes.json", evidence / "candidates.json"),
                     (package / "review/approved_keyframes.json", evidence / "approved.json")):
        if old.is_file():
            payload = read_json(old)
            for key in ("candidates", "items", "approved"):
                for entry in payload.get(key, []) or []:
                    if entry.get("image"): entry["image"] = "evidence/images/" + Path(entry["image"]).name
            atomic_write_json(new, payload); old.unlink()
    notes_input = package / "notes/notes_input.md"
    if notes_input.is_file():
        text = notes_input.read_text(encoding="utf-8").replace("../frames/keyframes/", "../evidence/images/").replace("../frames/contact_sheet.jpg", "../evidence/contact-sheet.jpg")
        notes_input.write_text(text, encoding="utf-8")
    artifacts = data["artifacts"]
    artifacts.update({"candidate_json": "evidence/candidates.json", "contact_sheet": "evidence/contact-sheet.jpg",
                      "approved_json": "evidence/approved.json", "notes_input": "notes/notes_input.md"})


def prepare(package: Path, workspace: WorkspaceConfig) -> dict[str, Any]:
    package = package.expanduser().resolve()
    if not (package / "manifest.json").is_file():
        return {"status": "needs_input", "package": str(package), "error": "managed package manifest is required"}
    try:
        package.relative_to(workspace.media.resolve())
    except ValueError:
        return {"status": "needs_input", "package": str(package), "error": "import the source into the configured workspace first"}
    with package_lock(package):
        data = read_json(package / "manifest.json"); kind = data.get("source_kind") or "video"
        try:
            if kind in {"video", "audio"}:
                transcript = _ensure_transcript(package, data)
                if not transcript:
                    return {"status": "needs_input", "package": str(package), "error": "source media is missing"}
                if kind == "video":
                    approved = _artifact(package, data, "approved_json")
                    if not approved or not approved.is_file() or read_json(approved).get("review_mode") not in {"model_only", "human"}:
                        _prepare_video(package, data, transcript)
                        atomic_write_json(package / "manifest.json", data)
                        return _pause(package, data, "evidence_select", data["artifacts"]["candidate_json"], data["artifacts"]["approved_json"], workspace)
                else:
                    _prepare_text_input(package, data, transcript, kind)
            elif kind == "transcript":
                transcript = _artifact(package, data, "transcript_srt")
                if not transcript or not parse_srt(transcript):
                    return {"status": "needs_input", "package": str(package), "error": "valid timed SRT is required"}
                _prepare_text_input(package, data, transcript, kind)
            elif kind == "document":
                document = _artifact(package, data, "source_document")
                if not document or not document.is_file():
                    return {"status": "needs_input", "package": str(package), "error": "source document is missing"}
                _prepare_text_input(package, data, document, kind)
            else:
                return {"status": "needs_input", "package": str(package), "error": f"unsupported source kind: {kind}"}
            notes = package / "notes/notes.zh-CN.md"
            data["artifacts"]["notes"] = "notes/notes.zh-CN.md"
            atomic_write_json(package / "manifest.json", data)
            if not notes.is_file() or not validate_goals(package, ["notes_zh"])["ok"]:
                return _pause(package, data, "notes_write", data["artifacts"]["notes_input"], "notes/notes.zh-CN.md", workspace)
            data.pop("pause", None); data.setdefault("stages", {})["notes_complete"] = {"result": "passed"}
            atomic_write_json(package / "manifest.json", data)
            return {"status": "ready", "package": str(package), "note": str(notes)}
        except Exception as exc:
            return {"status": "failed", "package": str(package), "error": str(exc)}


def finalize(package: Path, workspace: WorkspaceConfig) -> dict[str, Any]:
    package = package.expanduser().resolve()
    with package_lock(package):
        validation = validate_goals(package, ["notes_zh"])
        if not validation["ok"]:
            return {"status": "failed", "package": str(package), "validation": validation}
        from .obsidian_export import export_package
        from .library import update_package
        exported = export_package(package, workspace.generated)
        if not exported.get("ok"):
            return {"status": "failed", "package": str(package), "validation": validation, "export": exported}
        data = read_json(package / "manifest.json"); data.pop("pause", None)
        cleanup = _cleanup_working_artifacts(package, data)
        data.setdefault("stages", {})["notes_finalized"] = {"result": "passed"}
        atomic_write_json(package / "manifest.json", data)
        final_validation = validate_goals(package, ["notes_zh"])
        if not final_validation["ok"]:
            return {"status": "failed", "package": str(package), "validation": final_validation,
                    "export": exported, "cleanup": cleanup}
        indexed = update_package(package, workspace.media)
        if not indexed.get("ok"):
            return {"status": "failed", "package": str(package), "validation": final_validation, "export": exported, "library": indexed, "cleanup": cleanup}
        return {"status": "complete", "package": str(package), "note": data["artifacts"].get("notes"),
                "export": exported, "library": indexed, "validation": final_validation, "cleanup": cleanup}


def _cleanup_working_artifacts(package: Path, data: dict[str, Any]) -> dict[str, Any]:
    artifacts = data.get("artifacts", {}); removed: list[str] = []
    approved_path = _artifact(package, data, "approved_json")
    approved = read_json(approved_path) if approved_path and approved_path.is_file() else {"approved": []}
    adopted = {str(item.get("image")) for item in approved.get("approved", []) if item.get("image")}
    candidate_path = _artifact(package, data, "candidate_json")
    if candidate_path and candidate_path.is_file():
        candidates = read_json(candidate_path).get("candidates", [])
        for entry in candidates:
            raw = str(entry.get("image") or "")
            if raw and raw not in adopted:
                candidate = (package / raw).resolve()
                if package.resolve() in candidate.parents and candidate.is_file():
                    candidate.unlink(); removed.append(raw)
    for key in ("candidate_json", "contact_sheet", "notes_input"):
        path = _artifact(package, data, key)
        if path and path.is_file():
            path.unlink(); removed.append(str(path.relative_to(package)))
        artifacts.pop(key, None)
    for relative in ("review/contact_sheet.html", "review/codex_review_prompt.md"):
        path = package / relative
        if path.is_file(): path.unlink(); removed.append(relative)
    return {"removed": sorted(set(removed)), "adopted_images": sorted(adopted)}
