"""Import local source material into canonical workspace packages."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from .contracts import MEDIA_SCHEMA_VERSION
from .manifest import atomic_write_json, fingerprint, read_json
from .package_lock import package_lock
from .workspace import WorkspaceConfig


MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".mp3", ".m4a", ".wav", ".flac", ".aac"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}


def _content_id(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def import_source(source: Path, workspace: WorkspaceConfig, package: Path | None = None,
                  title: str | None = None) -> dict[str, Any]:
    source = source.expanduser().resolve()
    if source.is_dir() and (source / "manifest.json").is_file():
        return {"status": "ready", "package": str(source), "source_kind": read_json(source / "manifest.json").get("source_kind")}
    if not source.is_file():
        return {"status": "failed", "error": f"source does not exist: {source}"}
    suffix = source.suffix.lower()
    if suffix not in MEDIA_EXTENSIONS | {".srt", ".md", ".txt"}:
        return {"status": "needs_input", "error": "supported local inputs are media, SRT, Markdown, or UTF-8 text"}
    identity = _content_id(source)
    package = (package or workspace.media / "items" / "local" / identity).expanduser().resolve()
    try:
        package.relative_to(workspace.media.resolve())
    except ValueError:
        return {"status": "failed", "error": "package must be inside the configured Media root"}
    with package_lock(package):
        package.mkdir(parents=True, exist_ok=True)
        manifest_path = package / "manifest.json"
        data = read_json(manifest_path) if manifest_path.is_file() else {}
        artifacts = data.setdefault("artifacts", {})
        provenance = data.setdefault("provenance", {})
        if suffix in VIDEO_EXTENSIONS:
            kind, key, relative = "video", "source_video", "media/video.mp4"
        elif suffix in MEDIA_EXTENSIONS:
            kind, key, relative = "audio", "source_audio", "media/audio.source" + suffix
        elif suffix == ".srt":
            kind, key, relative = "transcript", "transcript_srt", "subtitles/transcript.source.srt"
        else:
            kind, key, relative = "document", "source_document", "source/document" + suffix
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or _content_id(target) != identity:
            shutil.copy2(source, target)
        package_kind = data.get("source_kind") or kind
        package_identity = data.get("identity") or identity
        data.update({"schema_version": MEDIA_SCHEMA_VERSION, "platform": data.get("platform") or "local", "identity": package_identity,
                     "title": title or data.get("title") or source.stem, "source_kind": package_kind,
                     "request": data.get("request") or {"type": "source", "source_kind": package_kind},
                     "artifacts": artifacts, "provenance": provenance})
        artifacts[key] = relative
        provenance[key] = {"kind": "local_import", "source_name": source.name, "fingerprint": identity}
        data.setdefault("stages", {})["source_ready"] = {"fingerprint": fingerprint([target]), "result": "passed"}
        atomic_write_json(manifest_path, data)
    return {"status": "ready", "package": str(package), "source_kind": package_kind, "artifacts": {key: relative}}
