"""Authoritative, source-version-pinned notes for workspace-v2.

Source snapshots own source facts.  This store contains only note bodies, adopted
attachments, citations, corrections and revision history, with source IDs as
foreign keys.  A note commit becomes visible by replacing one current pointer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from .command_response import response
from .manifest import atomic_write_json, read_json
from .package_lock import package_lock
from .source_registry import _load_snapshot
from .workspace import PORTABLE_SCHEMA_VERSION, WorkspaceConfig, WorkspaceError
from .timed_cues import cue_ranges
from .visual_approval import validate_approval, validate_published_review

SCHEMA_VERSION = 1
SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "schemas/notes-snapshot-v1.schema.json").read_text())


class NotesPublishInterrupted(OSError):
    def __init__(self, message: str, commit_id: str, stage: str):
        super().__init__(message)
        self.commit_id = commit_id
        self.stage = stage


def _schema_validate(value: Any, definition: str | None = None) -> None:
    schema = SCHEMA if definition is None else {"$ref": f"#/$defs/{definition}", "$defs": SCHEMA["$defs"]}
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)
    except ValidationError as exc:
        location = "/".join(map(str, exc.absolute_path)) or "<root>"
        raise ValueError(f"notes schema validation failed at {location}: {exc.message}") from exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_v2(config: WorkspaceConfig) -> tuple[Path, Path]:
    if config.schema_version != PORTABLE_SCHEMA_VERSION or not config.results or not config.local:
        raise WorkspaceError("workspace schema v2 is required for authoritative source notes")
    return config.results.resolve(strict=False), config.local.resolve(strict=False)


def _safe(root: Path, *parts: str) -> Path:
    root = root.resolve(strict=False)
    current = root
    for part in parts:
        current /= part
        if current.is_symlink():
            raise WorkspaceError(f"refusing symbolic link inside declared root: {current}")
    resolved = current.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise WorkspaceError(f"path escapes declared root: {current}")
    return current


def _roots(config: WorkspaceConfig) -> tuple[Path, Path, Path, Path]:
    results, _ = _require_v2(config)
    root = _safe(results, "source-notes")
    return (_safe(root, "objects"), _safe(root, "commits"),
            _safe(root, "current.json"), _safe(root, "candidates"))


def _sync_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _put(config: WorkspaceConfig, value: bytes) -> tuple[str, Path]:
    objects, _, _, _ = _roots(config)
    digest = _digest(value)
    path = _safe(objects, digest[:2], digest)
    if path.is_file():
        if _digest(path.read_bytes()) != digest:
            raise WorkspaceError(f"note object digest mismatch: {path}")
        _sync_dir(path.parent)
        return digest, path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(value); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    _sync_dir(path.parent)
    return digest, path


def _load_current(config: WorkspaceConfig) -> dict[str, Any]:
    _, commits, pointer, _ = _roots(config)
    if not pointer.is_file():
        return {"schema_version": 1, "workspace_id": config.workspace_id, "revision": 0,
                "commit_id": None, "notes": {}, "objects": {}}
    selected = read_json(pointer)
    _schema_validate(selected, "pointer")
    if selected["workspace_id"] != config.workspace_id:
        raise WorkspaceError("authoritative notes pointer belongs to another workspace")
    commit = commits / f"{selected.get('commit_id')}.json"
    if not commit.is_file() or _digest(commit.read_bytes()) != selected.get("manifest_sha256"):
        raise WorkspaceError("authoritative notes pointer does not resolve to a valid commit")
    value = read_json(commit)
    _validate_commit(config, value)
    return value


def _source_version(config: WorkspaceConfig, source_id: str, source_version: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot = _load_snapshot(config)
    package = snapshot.get("sources", {}).get(source_id)
    if not package:
        raise ValueError(f"unknown source_id: {source_id}")
    version_id = source_version or package["current_version"]
    version = package["versions"].get(version_id)
    if not version:
        raise ValueError(f"unknown source_version for {source_id}: {version_id}")
    return package, version


def _source_text(config: WorkspaceConfig, version: dict[str, Any]) -> str:
    if version.get("storage") != "object":
        raise ValueError("source tree notes require an explicit textual scope; direct document notes only in T04")
    assert config.results is not None
    digest = version["object_sha256"]
    path = _safe(config.results.resolve(strict=False), "objects", digest[:2], digest)
    if not path.is_file() or _digest(path.read_bytes()) != digest:
        raise WorkspaceError("registered source content object is missing or invalid")
    return path.read_text(encoding="utf-8")


def _paragraph_input(text: str) -> str:
    blocks = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
    return "\n\n".join(f"[paragraph:{index}]\n{block}" for index, block in enumerate(blocks, 1))


def _looks_like_srt(text: str) -> bool:
    return bool(re.search(r"(?m)^\d+\s*\n\d\d:\d\d:\d\d[,\.]\d{3}\s+-->\s+", text))


def prepare_note(config: WorkspaceConfig, source_id: str, source_version: str | None = None) -> dict[str, Any]:
    _, local = _require_v2(config)
    package, version = _source_version(config, source_id, source_version)
    text = _source_text(config, version)
    operation = "operation-" + _digest(f"notes.prepare:{config.workspace_id}:{source_id}:{version['source_version']}".encode())[:24]
    directory = _safe(local, "note-operations", operation)
    directory.mkdir(parents=True, exist_ok=True)
    srt = _looks_like_srt(text)
    locator_policy = ("Use only the real SRT cue ranges shown below. The transcript has "
                      "unverified_video_association unless formal association evidence is supplied."
                      if srt else
                      "Use heading or paragraph:N locators shown below. Do not invent timestamps or visual evidence.")
    model_input = directory / "notes-input.md"
    model_input.write_text(
        f"# Source-note model input\n\nsource_id: {source_id}\nsource_version: {version['source_version']}\n"
        f"title: {package['title']}\nlocator_policy: {locator_policy}\n\n"
        + (text if srt else _paragraph_input(text)), encoding="utf-8")
    return response(
        status="awaiting_model", workspace=str(config.config_path), operation_id=operation,
        result={"source_id": source_id, "source_version": version["source_version"],
                "model_input": str(model_input), "model_input_root": str(local)},
        validation={"source": "passed", "source_version": "passed"},
        provenance={"source_id": source_id, "source_version": version["source_version"]},
        next_action={"type": "model", "action": "write source-grounded note",
                     "request_schema": "source-note-finalize-request-v1"},
        artifact_refs=[str(model_input)],
    )


def associate_sources(config: WorkspaceConfig, source_id: str, related_source_id: str,
                      evidence: dict[str, Any] | None) -> dict[str, Any]:
    """Build a checked association record; finalize makes it authoritative with the note."""
    _, source = _source_version(config, source_id, None)
    _, related = _source_version(config, related_source_id, None)
    digest_matches = bool(isinstance(evidence, dict) and evidence.get("type") == "content_digest_equality"
                          and evidence.get("source_id") == source_id
                          and evidence.get("source_version") == source["source_version"]
                          and evidence.get("related_source_id") == related_source_id
                          and evidence.get("related_source_version") == related["source_version"]
                          and evidence.get("source_content_sha256") == source["content_sha256"]
                          and evidence.get("related_content_sha256") == related["content_sha256"]
                          and source["content_sha256"] == related["content_sha256"])
    association = {
        "status": "verified" if digest_matches else "unverified",
        "source_id": source_id, "source_version": source["source_version"],
        "related_source_id": related_source_id,
        "related_source_version": related["source_version"],
        "evidence": evidence if digest_matches else None,
    }
    if digest_matches:
        _, local = _require_v2(config)
        association_id = "association-" + _digest(json.dumps(
            association, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        association["association_id"] = association_id
        record = _safe(local, "source-associations", f"{association_id}.json")
        atomic_write_json(record, association)
    return response(status="completed", workspace=str(config.config_path), result={"association": association},
                    validation={"sources": "passed", "association_evidence": "passed" if digest_matches else "insufficient"},
                    provenance={"source_id": source_id, "source_version": source["source_version"]})


def _validate_request(config: WorkspaceConfig, value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    _schema_validate(value, "finalizeRequest")
    package, version = _source_version(config, value["source_id"], value["source_version"])
    validate_approval(config, value)
    source_text = _source_text(config, version)
    is_srt = _looks_like_srt(source_text)
    allowed = {"timestamp"} if is_srt else {"paragraph", "heading"}
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", source_text) if item.strip()]
    headings = {match.group(0).strip() for match in re.finditer(r"(?m)^#{1,6}\s+[^\n]+$", source_text)}
    source_cues = set(cue_ranges(source_text))
    for citation in value["citations"]:
        if citation.get("locator_type") not in allowed or not citation.get("locator") or not citation.get("claim"):
            raise ValueError("citation locator is absent or unsupported for this source")
        locator = citation["locator"]
        if citation["locator_type"] == "timestamp" and locator not in source_cues:
            raise ValueError("timestamp citation does not match a real SRT cue")
        if citation["locator_type"] == "paragraph":
            match = re.fullmatch(r"paragraph:([1-9]\d*)", locator)
            if not match or int(match.group(1)) > len(paragraphs):
                raise ValueError("paragraph citation does not match the registered source")
        if citation["locator_type"] == "heading" and locator not in headings:
            raise ValueError("heading citation does not match the registered source")
    for correction in value["corrections"]:
        if not isinstance(correction, dict) or not all(correction.get(key) for key in ("original", "corrected", "evidence")):
            raise ValueError("each correction must preserve the original wording, corrected wording, and evidence")
    for raw_attachment in value["attachments"]:
        attachment = Path(raw_attachment).expanduser().resolve()
        if not attachment.is_file():
            raise ValueError(f"adopted attachment is missing: {attachment}")
        if attachment.name not in value["markdown"]:
            raise ValueError(f"adopted attachment is not used by the note body: {attachment.name}")
    association = value["association"]
    if not isinstance(association, dict) or association.get("status") not in {
        "verified", "unverified", "not_applicable"
    }:
        raise ValueError("association status is invalid")
    if association["status"] == "verified":
        required_association = {"association_id", "source_id", "source_version", "related_source_id",
                                "related_source_version", "evidence"}
        if not required_association.issubset(association) or not association["evidence"]:
            raise ValueError("verified association requires a formal association record")
        if association["source_id"] != value["source_id"] or association["source_version"] != value["source_version"]:
            raise ValueError("association does not match the note source version")
        _source_version(config, association["related_source_id"], association["related_source_version"])
        canonical = {key: association[key] for key in association if key != "association_id"}
        expected_id = "association-" + _digest(json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        _, local = _require_v2(config)
        record = _safe(local, "source-associations", f"{expected_id}.json")
        if association["association_id"] != expected_id or not record.is_file() or read_json(record) != association:
            raise ValueError("verified association was not produced by source associate")
    return package, version


def _validate_commit(config: WorkspaceConfig, commit: dict[str, Any]) -> None:
    try:
        _schema_validate(commit)
    except ValueError as exc:
        raise WorkspaceError(str(exc)) from exc
    if commit["workspace_id"] != config.workspace_id:
        raise WorkspaceError("notes commit belongs to another workspace")
    identity_value = {key: value for key, value in commit.items() if key != "commit_id"}
    expected_id = "note-commit-" + _digest(json.dumps(
        identity_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    if commit["commit_id"] != expected_id or not isinstance(commit["revision"], int) or commit["revision"] < 1:
        raise WorkspaceError("notes commit identity or revision is invalid")
    objects, _, _, _ = _roots(config)
    source_snapshot = _load_snapshot(config)
    for digest, metadata in commit["objects"].items():
        path = _safe(objects, digest[:2], digest)
        if not path.is_file() or _digest(path.read_bytes()) != digest or path.stat().st_size != metadata.get("size"):
            raise WorkspaceError(f"invalid authoritative note object: {digest}")
    reachable: set[str] = set()
    for source_id, note in commit["notes"].items():
        source = source_snapshot.get("sources", {}).get(source_id)
        if not source or note.get("source_version") not in source.get("versions", {}):
            raise WorkspaceError(f"note source foreign key is invalid: {source_id}")
        refs = [note.get("body_object"), note.get("citations_object"), note.get("corrections_object"), *note.get("attachment_objects", [])]
        reachable.update(refs)
        if any(ref not in commit["objects"] for ref in refs):
            raise WorkspaceError(f"note object reachability is incomplete: {source_id}")
        if len(note.get("history", [])) != note.get("revision"):
            raise WorkspaceError(f"note history is incomplete: {source_id}")
        for expected_revision, entry in enumerate(note["history"], 1):
            history_refs = [entry.get("body_object"), entry.get("citations_object"),
                            entry.get("corrections_object"), *entry.get("attachment_objects", [])]
            reachable.update(history_refs)
            if entry.get("revision") != expected_revision or any(ref not in commit["objects"] for ref in history_refs):
                raise WorkspaceError(f"note history entry is invalid: {source_id}")
            if entry.get("source_version") not in source["versions"] or not entry.get("created_at"):
                raise WorkspaceError(f"note history source version is invalid: {source_id}")
            expected_kinds = {entry["body_object"]: "note_markdown", entry["citations_object"]: "citations",
                              entry["corrections_object"]: "corrections",
                              **{digest: "adopted_attachment" for digest in entry["attachment_objects"]}}
            if any(commit["objects"][digest]["kind"] != kind for digest, kind in expected_kinds.items()):
                raise WorkspaceError(f"note history object kind is invalid: {source_id}")
            _validate_published_association(source_snapshot, source_id, entry["source_version"], entry["association"])
            validate_published_review(entry.get("visual_review"), entry["attachment_objects"])
        latest = note["history"][-1]
        if note.get("visual_review") != latest.get("visual_review"):
            raise WorkspaceError("current visual approval does not match latest history")
        for key in ("source_version", "body_object", "citations_object", "corrections_object",
                    "attachment_objects", "association"):
            if note[key] != latest[key]:
                raise WorkspaceError(f"current note does not match its latest history revision: {source_id}")
        association = note.get("association")
        if not isinstance(association, dict) or association.get("status") not in {
            "verified", "unverified", "not_applicable"
        }:
            raise WorkspaceError(f"note association is invalid: {source_id}")
    if reachable != set(commit["objects"]):
        raise WorkspaceError("notes commit object set is not exactly reachable from revision history")


def _validate_published_association(source_snapshot: dict[str, Any], source_id: str,
                                    source_version: str, association: dict[str, Any]) -> None:
    if association["status"] != "verified":
        return
    source = source_snapshot["sources"][source_id]["versions"][source_version]
    related_package = source_snapshot.get("sources", {}).get(association.get("related_source_id"))
    related = (related_package or {}).get("versions", {}).get(association.get("related_source_version"))
    evidence = association.get("evidence") or {}
    canonical = {key: association[key] for key in association if key != "association_id"}
    expected_id = "association-" + _digest(json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    if (not related or association.get("source_id") != source_id
            or association.get("source_version") != source_version
            or evidence.get("type") != "content_digest_equality"
            or evidence.get("source_id") != source_id or evidence.get("source_version") != source_version
            or evidence.get("related_source_id") != association.get("related_source_id")
            or evidence.get("related_source_version") != association.get("related_source_version")
            or evidence.get("source_content_sha256") != source["content_sha256"]
            or evidence.get("related_content_sha256") != related["content_sha256"]
            or source["content_sha256"] != related["content_sha256"]
            or association.get("association_id") != expected_id):
        raise WorkspaceError(f"verified note association evidence is invalid: {source_id}")


def _publish(config: WorkspaceConfig, previous: dict[str, Any], notes: dict[str, Any], new_objects: dict[str, Any]) -> dict[str, Any]:
    _, commits, pointer, _ = _roots(config)
    manifest = {"schema_version": 1, "workspace_id": config.workspace_id,
                "parent_commit_id": previous.get("commit_id"),
                "revision": int(previous["revision"]) + 1, "created_at": _now(),
                "notes": notes, "objects": {**previous.get("objects", {}), **new_objects}}
    identity = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    manifest["commit_id"] = "note-commit-" + _digest(identity)
    _validate_commit(config, manifest)
    raw = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"
    commits.mkdir(parents=True, exist_ok=True)
    path = commits / f"{manifest['commit_id']}.json"
    if not path.exists():
        try:
            with path.open("xb") as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        except OSError:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
    elif path.read_bytes() != raw:
        raise WorkspaceError(f"immutable note commit differs from published content: {manifest['commit_id']}")
    try:
        _sync_dir(commits)
    except OSError as exc:
        raise NotesPublishInterrupted(str(exc), manifest["commit_id"], "commit_directory_fsync") from exc
    if os.environ.get("VIDEO_EXTRACT_NOTES_TEST_FAULT") == "before_publish":
        raise NotesPublishInterrupted("injected failure before notes publish", manifest["commit_id"], "before_pointer_write")
    pointer_value = {"schema_version": 1, "workspace_id": config.workspace_id,
                     "commit_id": manifest["commit_id"], "manifest_sha256": _digest(raw)}
    _schema_validate(pointer_value, "pointer")
    try:
        atomic_write_json(pointer, pointer_value)
    except OSError as exc:
        raise NotesPublishInterrupted(str(exc), manifest["commit_id"], "pointer_write") from exc
    try:
        _sync_dir(pointer.parent)
    except OSError as exc:
        raise NotesPublishInterrupted(str(exc), manifest["commit_id"], "pointer_directory_fsync") from exc
    if os.environ.get("VIDEO_EXTRACT_NOTES_TEST_FAULT") == "after_publish":
        raise NotesPublishInterrupted("injected failure after notes publish", manifest["commit_id"], "after_pointer_write")
    return manifest


def finalize_note(config: WorkspaceConfig, request: Path) -> dict[str, Any]:
    _require_v2(config)
    value = read_json(request)
    _, version = _validate_request(config, value)
    source_id = value["source_id"]
    _, _, _, candidates = _roots(config)
    assert config.results is not None
    try:
        with package_lock(_safe(config.results.resolve(strict=False), "source-notes")):
            validate_approval(config, value)
            previous = _load_current(config)
            existing = previous["notes"].get(source_id)
            note_revision = int(existing.get("revision", 0)) if existing else 0
            if value["expected_revision"] != note_revision:
                candidates.mkdir(parents=True, exist_ok=True)
                candidate = candidates / f"candidate-{uuid.uuid4()}.json"
                atomic_write_json(candidate, value)
                return response(status="awaiting_user", workspace=str(config.config_path),
                                result={"source_id": source_id, "candidate": str(candidate),
                                        "observed_revision": note_revision},
                                validation={"expected_revision": "conflict"},
                                next_action={"type": "user", "reason": "resolve note revision conflict"})
            prospective = {
                "body_object": _digest(value["markdown"].encode()),
                "citations_object": _digest(json.dumps(value["citations"], ensure_ascii=False, sort_keys=True).encode()),
                "corrections_object": _digest(json.dumps(value["corrections"], ensure_ascii=False, sort_keys=True).encode()),
                "attachment_objects": [_digest(Path(item).expanduser().resolve().read_bytes()) for item in value["attachments"]],
            }
            if existing and existing.get("source_version") == version["source_version"] and all(
                existing.get(key) == expected for key, expected in prospective.items()
            ) and existing.get("association") == value["association"] and existing.get("visual_review") == value.get("visual_review"):
                body_path = _safe(_roots(config)[0], prospective["body_object"][:2], prospective["body_object"])
                return response(status="completed", workspace=str(config.config_path),
                                result={"source_id": source_id, "source_version": version["source_version"],
                                        "revision": note_revision, "commit_id": previous["commit_id"],
                                        "history_length": len(existing["history"]), "note": str(body_path),
                                        "action": "reused"},
                                validation={"commit": "passed", "objects": "passed", "source": "passed"})
            values: list[tuple[str, bytes, str]] = [
                ("body", value["markdown"].encode(), "note_markdown"),
                ("citations", json.dumps(value["citations"], ensure_ascii=False, sort_keys=True).encode(), "citations"),
                ("corrections", json.dumps(value["corrections"], ensure_ascii=False, sort_keys=True).encode(), "corrections"),
            ]
            attachment_digests = []
            attachment_paths: list[Path] = []
            new_objects: dict[str, Any] = {}
            stored: dict[str, tuple[str, Path]] = {}
            for label, body, kind in values:
                digest, path = _put(config, body); stored[label] = (digest, path)
                new_objects[digest] = {"kind": kind, "size": len(body)}
            for raw_attachment in value["attachments"]:
                attachment = Path(raw_attachment).expanduser().resolve()
                body = attachment.read_bytes(); digest, stored_attachment = _put(config, body)
                attachment_digests.append(digest); attachment_paths.append(stored_attachment)
                new_objects[digest] = {"kind": "adopted_attachment", "size": len(body)}
            validate_published_review(value.get("visual_review"), attachment_digests)
            revision = note_revision + 1
            history_entry = {"revision": revision, "body_object": stored["body"][0],
                             "citations_object": stored["citations"][0],
                             "corrections_object": stored["corrections"][0],
                             "attachment_objects": attachment_digests,
                             "association": value["association"],
                             "source_version": version["source_version"], "created_at": _now()}
            history = [*(existing or {}).get("history", []), history_entry]
            if "visual_review" in value:
                history_entry["visual_review"] = value["visual_review"]
            note = {"source_id": source_id, "source_version": version["source_version"], "revision": revision,
                    "body_object": stored["body"][0], "citations_object": stored["citations"][0],
                    "corrections_object": stored["corrections"][0], "attachment_objects": attachment_digests,
                    "association": value["association"], "history": history}
            if "visual_review" in value:
                note["visual_review"] = value["visual_review"]
            published = _publish(config, previous, {**previous["notes"], source_id: note}, new_objects)
            return response(status="completed", workspace=str(config.config_path),
                            result={"source_id": source_id, "source_version": version["source_version"],
                                    "revision": revision, "commit_id": published["commit_id"],
                                    "history_length": len(history), "note": str(stored["body"][1]),
                                    "action": "published"},
                            validation={"commit": "passed", "objects": "passed", "source": "passed"},
                            provenance={"source_id": source_id, "source_version": version["source_version"],
                                        "visual_review": value.get("visual_review"),
                                        "note_commit_id": published["commit_id"]},
                            artifact_refs=[str(stored["body"][1]), *map(str, attachment_paths)])
    except OSError as exc:
        current = _load_current(config)
        target_commit = exc.commit_id if isinstance(exc, NotesPublishInterrupted) else None
        interrupted_stage = exc.stage if isinstance(exc, NotesPublishInterrupted) else None
        command = (f"video-extract notes finalize {shlex.quote(source_id)} --request {shlex.quote(str(request))} "
                   f"--workspace {shlex.quote(str(config.config_path))} --json")
        action_type = "retry"
        if target_commit:
            command = (f"video-extract notes reconcile --commit-id {shlex.quote(target_commit)} "
                       f"--workspace {shlex.quote(str(config.config_path))} --json")
            action_type = "reconcile"
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        result={"source_id": source_id, "visible_commit_id": current.get("commit_id"),
                                "target_commit_id": target_commit, "interrupted_stage": interrupted_stage},
                        validation={"commit": "uncertain", "durability": "unknown"}, diagnostics=[str(exc)],
                        next_action={"type": action_type, "source_id": source_id,
                                     "commit_id": target_commit, "stage": interrupted_stage, "command": command})


def audit_notes(config: WorkspaceConfig) -> dict[str, Any]:
    _, commits, _, _ = _roots(config)
    current = _load_current(config)
    checked = 0
    by_id: dict[str, dict[str, Any]] = {}
    for path in sorted(commits.glob("note-commit-*.json")) if commits.exists() else []:
        value = read_json(path)
        _validate_commit(config, value)
        if path.name != f"{value['commit_id']}.json":
            raise WorkspaceError(f"notes commit filename/identity mismatch: {path.name}")
        by_id[value["commit_id"]] = value
        checked += 1
    visited: set[str] = set()
    cursor = current.get("commit_id")
    while cursor:
        if cursor in visited or cursor not in by_id:
            raise WorkspaceError("current notes commit chain is cyclic or incomplete")
        value = by_id[cursor]; visited.add(cursor)
        parent_id = value["parent_commit_id"]
        if parent_id is None:
            if value["revision"] != 1:
                raise WorkspaceError("root notes commit revision is not 1")
        else:
            parent = by_id.get(parent_id)
            if not parent or parent["revision"] + 1 != value["revision"]:
                raise WorkspaceError(f"notes commit parent/revision chain is invalid: {value['commit_id']}")
        cursor = parent_id
    if visited != set(by_id):
        raise WorkspaceError("notes commit is not on the current commit chain")
    objects, _, _, _ = _roots(config)
    notes = [{"source_id": source_id, "source_version": note["source_version"],
              "revision": note["revision"], "note": str(_safe(objects, note["body_object"][:2], note["body_object"])),
              "commit_id": current["commit_id"]} for source_id, note in sorted(current["notes"].items())]
    return response(status="completed", workspace=str(config.config_path),
                    result={"commit_count": checked, "notes": notes},
                    validation={"commits": "passed", "objects": "passed", "sources": "passed"})


def reconcile_notes(config: WorkspaceConfig, target_commit_id: str | None = None) -> dict[str, Any]:
    objects, commits, pointer, _ = _roots(config)
    assert config.results is not None
    with package_lock(_safe(config.results.resolve(strict=False), "source-notes")):
        return _reconcile_notes_locked(config, objects, commits, pointer, target_commit_id)


def _reconcile_notes_locked(config: WorkspaceConfig, objects: Path, commits: Path, pointer: Path,
                            target_commit_id: str | None) -> dict[str, Any]:
    if target_commit_id:
        target_path = commits / f"{target_commit_id}.json"
        if not target_path.is_file():
            raise WorkspaceError(f"target notes commit is missing: {target_commit_id}")
        target = read_json(target_path)
        _validate_commit(config, target)
        if target["commit_id"] != target_commit_id or target_path.name != f"{target_commit_id}.json":
            raise WorkspaceError("target notes commit identity is invalid")
        current = _load_current(config)
        if current.get("commit_id") != target_commit_id:
            if target["parent_commit_id"] != current.get("commit_id") or target["revision"] != current["revision"] + 1:
                _, _, _, candidates = _roots(config)
                candidates.mkdir(parents=True, exist_ok=True)
                candidate = candidates / f"recovery-{target_commit_id}.json"
                atomic_write_json(candidate, {"target_commit_id": target_commit_id,
                                              "observed_commit_id": current.get("commit_id"),
                                              "target_parent_commit_id": target["parent_commit_id"]})
                return response(status="awaiting_user", workspace=str(config.config_path),
                                result={"target_commit_id": target_commit_id,
                                        "observed_commit_id": current.get("commit_id"),
                                        "candidate": str(candidate)},
                                validation={"expected_revision": "conflict"},
                                next_action={"type": "user", "reason": "resolve competing note commits"})
            raw = target_path.read_bytes()
            pointer_value = {"schema_version": 1, "workspace_id": config.workspace_id,
                             "commit_id": target_commit_id, "manifest_sha256": _digest(raw)}
            _schema_validate(pointer_value, "pointer")
            atomic_write_json(pointer, pointer_value)
            _sync_dir(pointer.parent)
    audited = audit_notes(config)
    for path in sorted(objects.glob("*/*")) if objects.exists() else []:
        _sync_file(path)
    for shard in sorted(objects.glob("*")) if objects.exists() else []:
        if shard.is_dir():
            _sync_dir(shard)
    if objects.exists():
        _sync_dir(objects)
    for commit in sorted(commits.glob("*.json")) if commits.exists() else []:
        _sync_file(commit)
    if commits.exists():
        _sync_dir(commits)
    _sync_file(pointer)
    _sync_dir(pointer.parent)
    audited["result"]["durability"] = "confirmed"
    audited["result"]["commit_id"] = audited["result"]["notes"][0]["commit_id"] if audited["result"]["notes"] else None
    audited["validation"]["durability"] = "passed"
    return audited
