"""Versioned source registration on the workspace-v2 snapshot store."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from .command_response import response
from .manifest import atomic_write_json, read_json
from .package_lock import package_lock
from .workspace import PORTABLE_SCHEMA_VERSION, WorkspaceConfig, WorkspaceError

PACKAGE_SCHEMA_VERSION = 6
SNAPSHOT_SCHEMA_VERSION = 1
SCHEMA_ROOT = Path(__file__).resolve().parent.parent / "schemas"


def _schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))


_SCHEMAS = {name: _schema(name) for name in (
    "source-package-v6.schema.json", "snapshot-v1.schema.json", "current-pointer-v1.schema.json"
)}
_SCHEMA_REGISTRY = Registry().with_resources(
    (schema["$id"], Resource.from_contents(schema)) for schema in _SCHEMAS.values()
)


def _validate_schema(name: str, value: Any) -> None:
    try:
        Draft202012Validator(_SCHEMAS[name], registry=_SCHEMA_REGISTRY,
                             format_checker=FormatChecker()).validate(value)
    except ValidationError as exc:
        location = "/".join(map(str, exc.absolute_path)) or "<root>"
        raise WorkspaceError(f"{name} validation failed at {location}: {exc.message}") from exc


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_object_barrier(path: Path) -> None:
    """Idempotently make an immutable object's directory entries durable."""
    _sync_directory(path.parent)
    _sync_directory(path.parent.parent)


def _sync_commit_barrier(commits: Path) -> None:
    """Idempotently make commit-manifest directory entries durable."""
    _sync_directory(commits)
    _sync_directory(commits.parent)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _directory_manifest(root: Path) -> tuple[str, list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts):
        relative = path.relative_to(root).as_posix()
        content_digest = _sha256_bytes(path.read_bytes())
        entries.append({"path": relative, "sha256": content_digest, "size": path.stat().st_size})
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(content_digest.encode())
        digest.update(b"\0")
    return digest.hexdigest(), entries


def _git_basis(path: Path) -> dict[str, Any]:
    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    top = run("rev-parse", "--show-toplevel")
    if top.returncode:
        return {"git_commit": None, "working_tree_dirty": None}
    commit = run("rev-parse", "HEAD")
    dirty = run("status", "--porcelain", "--untracked-files=all")
    return {"git_commit": commit.stdout.strip() if commit.returncode == 0 else None,
            "working_tree_dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None}


def _require_v2(config: WorkspaceConfig) -> None:
    if config.schema_version != PORTABLE_SCHEMA_VERSION or not all(
        (config.results, config.sources, config.derived, config.local)
    ):
        raise WorkspaceError("workspace schema v1 is not accepted by source v6; run explicit migration")


def _safe_path(root: Path, *parts: str) -> Path:
    root = root.resolve(strict=False)
    candidate = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise WorkspaceError(f"refusing symbolic link inside declared results root: {current}")
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise WorkspaceError(f"write escapes declared results root: {candidate}")
    return candidate


def _store_roots(config: WorkspaceConfig) -> tuple[Path, Path, Path, Path]:
    assert config.results is not None
    return (_safe_path(config.results, "objects"), _safe_path(config.results, "commits"),
            _safe_path(config.results, "current.json"), _safe_path(config.results, "candidates"))


def _empty_snapshot() -> dict[str, Any]:
    return {"schema_version": SNAPSHOT_SCHEMA_VERSION, "commit_id": None, "parent_commit_id": None,
            "revision": 0, "created_at": None, "sources": {}, "objects": {}}


def _load_snapshot(config: WorkspaceConfig) -> dict[str, Any]:
    _, commits, pointer, _ = _store_roots(config)
    if not pointer.is_file():
        return _empty_snapshot()
    pointer_data = read_json(pointer)
    _validate_schema("current-pointer-v1.schema.json", pointer_data)
    commit_id = pointer_data.get("commit_id")
    manifest_path = _safe_path(commits.parent, "commits", f"{commit_id}.json")
    if not manifest_path.is_file():
        raise WorkspaceError(f"published snapshot manifest is missing: {commit_id}")
    snapshot = read_json(manifest_path)
    if snapshot.get("commit_id") != commit_id:
        raise WorkspaceError("published snapshot manifest failed schema/identity validation")
    expected_manifest = pointer_data.get("manifest_sha256")
    if expected_manifest != _sha256_bytes(manifest_path.read_bytes()):
        raise WorkspaceError("published snapshot manifest digest mismatch")
    _validate_schema("snapshot-v1.schema.json", snapshot)
    _validate_source_packages(snapshot)
    return snapshot


def _validate_source_packages(snapshot: dict[str, Any]) -> None:
    for source_id, package in snapshot["sources"].items():
        _validate_schema("source-package-v6.schema.json", package)
        if package["source_id"] != source_id or package["current_version"] not in package["versions"]:
            raise WorkspaceError(f"source-package-v6.schema.json identity/current version mismatch: {source_id}")
        for version_id, version in package["versions"].items():
            if version["source_version"] != version_id or version["kind"] != package["kind"]:
                raise WorkspaceError(f"source-package-v6.schema.json version identity/kind mismatch: {version_id}")
            if version_id != "source-version-" + version["content_sha256"]:
                raise WorkspaceError(f"source-package-v6.schema.json source version/content definition mismatch: {version_id}")
            if version["version_basis"]["content_sha256"] != version["content_sha256"]:
                raise WorkspaceError(f"source-package-v6.schema.json version digest mismatch: {version_id}")
            if version["storage"] == "object" and "object_sha256" not in version:
                raise WorkspaceError(f"source-package-v6.schema.json object storage lacks object_sha256: {version_id}")
            if version.get("object_sha256") and version["object_sha256"] != version["content_sha256"]:
                raise WorkspaceError(f"source-package-v6.schema.json object/content digest mismatch: {version_id}")


def _write_object(config: WorkspaceConfig, value: bytes) -> tuple[str, Path]:
    objects, _, _, _ = _store_roots(config)
    digest = _sha256_bytes(value)
    path = _safe_path(objects.parent, "objects", digest[:2], digest)
    if path.is_file():
        if _sha256_bytes(path.read_bytes()) != digest:
            raise WorkspaceError(f"immutable object digest mismatch: {path}")
        _sync_object_barrier(path)
        return digest, path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _sync_object_barrier(path)
    return digest, path


def _locations_path(config: WorkspaceConfig) -> Path:
    assert config.local is not None
    root = config.local.resolve(strict=False)
    path = root / "source-locations.json"
    if path.resolve(strict=False).parent != root:
        raise WorkspaceError("location map escapes declared local root")
    return path


def _locations(config: WorkspaceConfig) -> dict[str, str]:
    path = _locations_path(config)
    return read_json(path) if path.is_file() else {}


def _save_locations(config: WorkspaceConfig, values: dict[str, str]) -> None:
    atomic_write_json(_locations_path(config), values)


def _source_at_location(snapshot: dict[str, Any], locations: dict[str, str], path: Path) -> str | None:
    resolved = str(path.resolve())
    return next((source_id for source_id, value in locations.items()
                 if value == resolved and source_id in snapshot["sources"]), None)


def _operation(action: str, config: WorkspaceConfig, source_id: str, source_version: str | None = None) -> str:
    raw = json.dumps({"action": action, "workspace_id": config.workspace_id,
                      "source_id": source_id, "source_version": source_version}, sort_keys=True)
    return "operation-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _recovery_source_id(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.startswith("source-"):
        raise ValueError("recovery source_id must use the source-<UUIDv4> identity form")
    try:
        parsed = uuid.UUID(value.removeprefix("source-"))
    except ValueError as exc:
        raise ValueError("recovery source_id must contain a valid UUIDv4") from exc
    canonical = "source-" + str(parsed)
    if parsed.version != 4 or value != canonical:
        raise ValueError("recovery source_id must be a canonical lowercase UUIDv4 identity")
    return canonical


def _candidate(config: WorkspaceConfig, value: dict[str, Any]) -> Path:
    _, _, _, candidates = _store_roots(config)
    path = _safe_path(candidates.parent, "candidates", f"candidate-{uuid.uuid4()}.json")
    atomic_write_json(path, value)
    return path


def _publish(config: WorkspaceConfig, previous: dict[str, Any], sources: dict[str, Any],
             object_refs: dict[str, Any]) -> dict[str, Any]:
    _, commits, pointer, _ = _store_roots(config)
    revision = int(previous["revision"]) + 1
    manifest = {"schema_version": SNAPSHOT_SCHEMA_VERSION, "parent_commit_id": previous.get("commit_id"),
                "revision": revision, "created_at": _now(), "sources": sources,
                "objects": {**previous.get("objects", {}), **object_refs}}
    content = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    commit_id = "commit-" + _sha256_bytes(content)
    manifest["commit_id"] = commit_id
    _validate_schema("snapshot-v1.schema.json", manifest)
    _validate_source_packages(manifest)
    _validate_snapshot_objects(config, manifest)
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"
    manifest_digest, _ = _write_object(config, manifest_bytes)
    commits.mkdir(parents=True, exist_ok=True)
    commit_path = _safe_path(commits.parent, "commits", f"{commit_id}.json")
    if commit_path.exists():
        if commit_path.read_bytes() != manifest_bytes:
            raise WorkspaceError(f"immutable commit manifest differs from published content: {commit_id}")
    else:
        with commit_path.open("xb") as stream:
            stream.write(manifest_bytes); stream.flush(); os.fsync(stream.fileno())
    _sync_commit_barrier(commits)
    if os.environ.get("VIDEO_EXTRACT_TEST_FAULT") == "before_publish":
        raise OSError("injected failure before snapshot publish")
    pointer_value = {"schema_version": SNAPSHOT_SCHEMA_VERSION, "commit_id": commit_id,
                     "manifest_sha256": manifest_digest}
    _validate_schema("current-pointer-v1.schema.json", pointer_value)
    atomic_write_json(pointer, pointer_value)
    _sync_directory(pointer.parent)
    if os.environ.get("VIDEO_EXTRACT_TEST_FAULT") == "after_publish":
        raise OSError("injected failure after snapshot publish")
    return manifest


def _capture(path: Path) -> tuple[str, str, bytes | None, dict[str, Any], list[dict[str, Any]]]:
    if path.is_file():
        content = _file_bytes(path)
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("source register accepts UTF-8 text files or source directories") from exc
        digest = _sha256_bytes(content)
        return "document", digest, content, {"content_sha256": digest}, []
    if path.is_dir():
        digest, entries = _directory_manifest(path)
        return "code", digest, None, {**_git_basis(path), "content_sha256": digest}, entries
    raise FileNotFoundError(f"source does not exist: {path}")


def register(config: WorkspaceConfig, path: Path, title: str | None = None,
             expected_revision: int | None = None, explicit_source_id: str | None = None,
             provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    _require_v2(config)
    path = path.expanduser().resolve()
    kind, digest, body, basis, entries = _capture(path)
    source_version = "source-version-" + digest
    recovery_source_id = _recovery_source_id(explicit_source_id)
    source_id = "unresolved"
    assert config.results is not None
    try:
        with package_lock(config.results):
            snapshot = _load_snapshot(config)
            locations = _locations(config)
            located_source_id = _source_at_location(snapshot, locations, path)
            if recovery_source_id and located_source_id and recovery_source_id != located_source_id:
                raise ValueError("recovery source_id conflicts with the source already registered at this location")
            if recovery_source_id in snapshot["sources"]:
                trusted_location = locations.get(recovery_source_id)
                if trusted_location is None:
                    raise ValueError(
                        "committed recovery source_id has no trusted local mapping; use source relocate "
                        "with same-version evidence or a formal association flow"
                    )
                if str(Path(trusted_location).expanduser().resolve()) != str(path):
                    raise ValueError(
                        "committed recovery source_id belongs to another canonical path; use source relocate "
                        "with same-version evidence or a formal association flow"
                    )
            source_id = located_source_id or recovery_source_id or "source-" + str(uuid.uuid4())
            current = snapshot["sources"].get(source_id)
            proposed = {"source_version": source_version, "content_sha256": digest, "kind": kind,
                        "captured_at": _now(), "version_basis": basis, "entries": entries,
                        "storage": "object" if body is not None else "reference"}
            if provenance is not None:
                proposed["provenance"] = provenance
            if expected_revision is not None and expected_revision != snapshot["revision"]:
                if body is not None:
                    object_digest, _ = _write_object(config, body)
                    proposed["object_sha256"] = object_digest
                candidate = _candidate(config, {"schema_version": PACKAGE_SCHEMA_VERSION,
                                                "source_id": source_id, "expected_revision": expected_revision,
                                                "observed_revision": snapshot["revision"],
                                                "proposed_version": proposed})
                return response(status="awaiting_user", workspace=str(config.config_path),
                                operation_id=_operation("source.register", config, source_id, source_version),
                                result={"source_id": source_id, "candidate": str(candidate),
                                        "observed_revision": snapshot["revision"]},
                                validation={"expected_revision": "conflict"},
                                next_action={"type": "user", "reason": "resolve source revision conflict"})
            versions = dict(current.get("versions", {})) if current else {}
            if source_version in versions:
                if provenance is not None and versions[source_version].get("provenance") != provenance:
                    raise ValueError("registered source version provenance is immutable")
                if current["current_version"] == source_version:
                    locations[source_id] = str(path); _save_locations(config, locations)
                    return _result(config, snapshot, source_id, versions[source_version], path, "reused")
                package = {**current, "current_version": source_version}
                published = _publish(config, snapshot, {**snapshot["sources"], source_id: package}, {})
                locations[source_id] = str(path); _save_locations(config, locations)
                return _result(config, published, source_id, versions[source_version], path, "restored_version")
            object_refs: dict[str, Any] = {}
            if body is not None:
                object_digest, _ = _write_object(config, body)
                proposed["object_sha256"] = object_digest
                object_refs[object_digest] = {"kind": "source_content", "size": len(body)}
            package = {"schema_version": PACKAGE_SCHEMA_VERSION, "source_id": source_id,
                       "kind": kind, "title": title or (current or {}).get("title") or path.name,
                       "current_version": source_version,
                       "versions": {**versions, source_version: proposed}}
            sources = {**snapshot["sources"], source_id: package}
            published = _publish(config, snapshot, sources, object_refs)
            locations[source_id] = str(path); _save_locations(config, locations)
            return _result(config, published, source_id, proposed, path, "registered")
    except OSError as exc:
        observed: dict[str, Any] | None = None
        recovery_revision: int | None = None
        try:
            current = _load_snapshot(config)
            recovery_revision = current["revision"]
            package = current.get("sources", {}).get(source_id)
            if package and package.get("current_version") == source_version:
                observed = {"workspace_id": config.workspace_id, "source_id": source_id,
                            "source_version": source_version, "revision": current["revision"],
                            "commit_id": current["commit_id"],
                            "logical_visibility": "current", "durability": "unknown"}
        except (OSError, ValueError, WorkspaceError):
            pass
        if observed:
            recovery_command = (f"video-extract source reconcile {shlex.quote(source_id)} "
                                f"--source-version {shlex.quote(source_version)} "
                                f"--workspace {shlex.quote(str(config.config_path))} --json")
            recovery_type = "reconcile"
        else:
            revision_argument = f" --expected-revision {recovery_revision}" if recovery_revision is not None else ""
            recovery_command = (f"video-extract source register {shlex.quote(str(path))}"
                                f" --source-id {shlex.quote(source_id)}")
            if title is not None:
                recovery_command += f" --title {shlex.quote(title)}"
            recovery_command += (f"{revision_argument} --workspace "
                                f"{shlex.quote(str(config.config_path))} --json")
            recovery_type = "retry"
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        operation_id=_operation("source.register", config, source_id, source_version),
                        result=observed or {"workspace_id": config.workspace_id, "source_id": source_id,
                                           "source_version": source_version,
                                           "logical_visibility": "not_current", "durability": "unknown"},
                        validation={"snapshot": "uncertain", "durability": "unknown"},
                        diagnostics=[str(exc)],
                        next_action={"type": recovery_type, "command": recovery_command,
                                     "source_id": source_id, "source_version": source_version})


def _validate_snapshot_objects(config: WorkspaceConfig, snapshot: dict[str, Any]) -> None:
    objects, _, _, _ = _store_roots(config)
    for digest, record in snapshot.get("objects", {}).items():
        path = _safe_path(objects.parent, "objects", digest[:2], digest)
        if not path.is_file() or _sha256_bytes(path.read_bytes()) != digest:
            raise WorkspaceError(f"snapshot object is missing or corrupt: {digest}")
        if path.stat().st_size != record["size"]:
            raise WorkspaceError(f"snapshot object size record is inconsistent: {digest}")
    for package in snapshot.get("sources", {}).values():
        for version_id, version in package["versions"].items():
            expected_version = "source-version-" + version["content_sha256"]
            if version_id != expected_version or version["source_version"] != expected_version:
                raise WorkspaceError(f"source version/content digest definition mismatch: {version_id}")
            object_digest = version.get("object_sha256")
            if object_digest and object_digest not in snapshot["objects"]:
                raise WorkspaceError(
                    f"source version object is not reachable through snapshot.objects: {version_id}"
                )


def reconcile(config: WorkspaceConfig, source_id: str, source_version: str) -> dict[str, Any]:
    """Confirm the visible commit and complete its directory durability barriers."""
    _require_v2(config)
    snapshot = _load_snapshot(config)
    package = snapshot["sources"].get(source_id)
    operation_id = _operation("source.register", config, source_id, source_version)
    if not package or source_version not in package.get("versions", {}):
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        operation_id=operation_id,
                        result={"source_id": source_id, "source_version": source_version,
                                "logical_visibility": "not_current", "durability": "unknown"},
                        validation={"snapshot": "passed", "durability": "not_applicable"},
                        diagnostics=["the requested source version is not present in the current snapshot"],
                        next_action={"type": "retry", "reason": "repeat source register with expected_revision"})
    if package["current_version"] != source_version:
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        operation_id=operation_id,
                        result={"source_id": source_id, "source_version": source_version,
                                "logical_visibility": "historical", "durability": "unknown"},
                        validation={"snapshot": "passed", "durability": "not_applicable"},
                        diagnostics=["the requested source version is not current; no durability claim was made"])
    _validate_snapshot_objects(config, snapshot)
    objects, commits, pointer, _ = _store_roots(config)
    for digest in snapshot["objects"]:
        _sync_object_barrier(_safe_path(objects.parent, "objects", digest[:2], digest))
    _sync_commit_barrier(commits)
    _sync_directory(pointer.parent)
    return response(status="completed", workspace=str(config.config_path), operation_id=operation_id,
                    result={"workspace_id": config.workspace_id, "source_id": source_id,
                            "source_version": source_version, "revision": snapshot["revision"],
                            "commit_id": snapshot["commit_id"], "logical_visibility": "current",
                            "durability": "confirmed"},
                    validation={"snapshot": "passed", "objects": "passed", "durability": "passed"},
                    provenance={"commit_id": snapshot["commit_id"], "source_version": source_version})


def audit(config: WorkspaceConfig) -> dict[str, Any]:
    """Read-only validation used by workspace doctor."""
    _require_v2(config)
    snapshot = _load_snapshot(config)
    _validate_snapshot_objects(config, snapshot)
    return {"ok": True, "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "commit_id": snapshot.get("commit_id"), "revision": snapshot["revision"],
            "sources": len(snapshot["sources"]), "objects": len(snapshot["objects"])}


def verify(config: WorkspaceConfig, source_id: str, source_version: str | None = None) -> dict[str, Any]:
    _require_v2(config)
    snapshot = _load_snapshot(config)
    package = snapshot["sources"].get(source_id)
    if not package:
        return response(status="missing_input", workspace=str(config.config_path),
                        validation={"source": "failed"}, diagnostics=[f"unknown source_id: {source_id}"])
    version_id = source_version or package["current_version"]
    version = package["versions"].get(version_id)
    if not version:
        return response(status="missing_input", workspace=str(config.config_path),
                        validation={"source_version": "failed"}, diagnostics=[f"unknown source_version: {version_id}"])
    _validate_snapshot_objects(config, snapshot)
    locations = _locations(config)
    location = locations.get(source_id)
    availability = "missing"
    content: str | None = None
    if version.get("object_sha256"):
        objects, _, _, _ = _store_roots(config)
        body = _safe_path(objects.parent, "objects", version["object_sha256"][:2], version["object_sha256"]).read_bytes()
        content = body.decode("utf-8")
        availability = "available_from_results"
    elif location and Path(location).exists():
        _, current_digest, _, _, _ = _capture(Path(location))
        availability = "available_at_location" if current_digest == version["content_sha256"] else "version_mismatch"
    result = {"source_id": source_id, "source_version": version_id, "kind": package["kind"],
              "title": package["title"], "revision": snapshot["revision"],
              "commit_id": snapshot["commit_id"], "location": location,
              "availability": availability, "content": content,
              "package_schema_version": PACKAGE_SCHEMA_VERSION,
              "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
              "version_basis": version["version_basis"], "storage": version["storage"],
              "entries": version.get("entries", [])}
    result["current_version"] = package["current_version"]
    result["version_state"] = "current" if version_id == package["current_version"] else "historical"
    result["change_check"] = "current" if result["version_state"] == "current" else "needs_review"
    if result["version_state"] == "historical":
        current_version = package["versions"][package["current_version"]]
        before = {entry["path"]: entry["sha256"] for entry in version.get("entries", [])}
        after = {entry["path"]: entry["sha256"] for entry in current_version.get("entries", [])}
        if package["kind"] == "code":
            result["change_summary"] = {
                "added": sorted(after.keys() - before.keys()),
                "modified": sorted(path for path in before.keys() & after.keys() if before[path] != after[path]),
                "removed": sorted(before.keys() - after.keys()),
            }
        else:
            result["change_summary"] = {"added": [], "modified": ["<document>"], "removed": []}
    else:
        result["change_summary"] = {"added": [], "modified": [], "removed": []}
    result["provenance"] = version.get("provenance")
    result["provenance_status"] = "recorded" if version.get("provenance") else "unknown"
    return response(status="completed", workspace=str(config.config_path),
                    operation_id=_operation("source.verify", config, source_id, version_id), result=result,
                    validation={"package": "passed", "snapshot": "passed", "objects": "passed"},
                    provenance={"commit_id": snapshot["commit_id"], "source_version": version_id})


def relocate(config: WorkspaceConfig, source_id: str, path: Path, expected_revision: int) -> dict[str, Any]:
    _require_v2(config)
    path = path.expanduser().resolve()
    snapshot = _load_snapshot(config)
    package = snapshot["sources"].get(source_id)
    if not package:
        return response(status="missing_input", workspace=str(config.config_path),
                        validation={"source": "failed"}, diagnostics=[f"unknown source_id: {source_id}"])
    if expected_revision != snapshot["revision"]:
        candidate = _candidate(config, {"schema_version": PACKAGE_SCHEMA_VERSION, "source_id": source_id,
                                        "proposed_location": str(path), "expected_revision": expected_revision,
                                        "observed_revision": snapshot["revision"]})
        return response(status="awaiting_user", workspace=str(config.config_path),
                        operation_id=_operation("source.relocate", config, source_id),
                        result={"candidate": str(candidate)}, validation={"expected_revision": "conflict"},
                        next_action={"type": "user", "reason": "resolve source revision conflict"})
    try:
        _, digest, _, _, _ = _capture(path)
    except FileNotFoundError as exc:
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        validation={"source_version": "failed"}, diagnostics=[str(exc)])
    version = package["versions"][package["current_version"]]
    if digest != version["content_sha256"]:
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        operation_id=_operation("source.relocate", config, source_id),
                        result={"source_id": source_id, "availability": "version_mismatch",
                                "expected_sha256": version["content_sha256"], "actual_sha256": digest},
                        validation={"source_version": "failed"},
                        diagnostics=["relocation target is a different source version"])
    assert config.results is not None
    with package_lock(config.results):
        current = _load_snapshot(config)
        if current["revision"] != expected_revision:
            candidate = _candidate(config, {"schema_version": PACKAGE_SCHEMA_VERSION,
                                            "source_id": source_id, "proposed_location": str(path),
                                            "expected_revision": expected_revision,
                                            "observed_revision": current["revision"]})
            return response(status="awaiting_user", workspace=str(config.config_path),
                            operation_id=_operation("source.relocate", config, source_id),
                            result={"candidate": str(candidate)},
                            validation={"expected_revision": "conflict"},
                            next_action={"type": "user", "reason": "resolve source revision conflict"})
        published = _publish(config, current, current["sources"], {})
        locations = _locations(config); locations[source_id] = str(path); _save_locations(config, locations)
    return response(status="completed", workspace=str(config.config_path),
                    operation_id=_operation("source.relocate", config, source_id, package["current_version"]),
                    result={"source_id": source_id, "source_version": package["current_version"],
                            "location": str(path), "revision": published["revision"],
                            "commit_id": published["commit_id"]},
                    validation={"source_version": "passed", "snapshot": "passed"},
                    provenance={"commit_id": published["commit_id"]})


def _result(config: WorkspaceConfig, snapshot: dict[str, Any], source_id: str,
            version: dict[str, Any], path: Path, action: str) -> dict[str, Any]:
    package = snapshot["sources"][source_id]
    result = {"source_id": source_id, "source_version": version["source_version"],
              "title": package["title"], "kind": version["kind"], "version_basis": version["version_basis"],
              "storage": version["storage"], "location": str(path), "action": action,
              "revision": snapshot["revision"], "commit_id": snapshot["commit_id"],
              "package_schema_version": PACKAGE_SCHEMA_VERSION,
              "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION}
    return response(status="completed", workspace=str(config.config_path),
                    operation_id=_operation("source.register", config, source_id, version["source_version"]),
                    result=result, validation={"package": "passed", "snapshot": "passed", "objects": "passed"},
                    provenance={"commit_id": snapshot["commit_id"], "source_version": version["source_version"]})
