"""Consistent, result-only backup generations and isolated restore."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .command_response import engineering_revision, response
from .manifest import atomic_write_json, read_json
from .package_lock import PackageBusyError, package_lock
from .workspace import PORTABLE_SCHEMA_VERSION, WorkspaceConfig, WorkspaceError

SCHEMA_VERSION = 1
SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "schemas/backup-manifest-v1.schema.json").read_text())
MANIFEST = "backup-manifest.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe(root: Path, relative: str) -> Path:
    if not relative or relative.startswith("/") or "\\" in relative:
        raise WorkspaceError(f"unsafe backup path: {relative!r}")
    parts = Path(relative).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise WorkspaceError(f"unsafe backup path: {relative!r}")
    current = root
    for part in parts:
        current /= part
        if current.is_symlink():
            raise WorkspaceError(f"refusing symbolic link in backup path: {current}")
    resolved = current.resolve(strict=False)
    base = root.resolve(strict=False)
    if base not in resolved.parents:
        raise WorkspaceError(f"backup path escapes root: {relative}")
    return current


def _validate_manifest(value: Any) -> None:
    errors = list(Draft202012Validator(SCHEMA, format_checker=FormatChecker()).iter_errors(value))
    if errors:
        error = errors[0]
        location = "/".join(map(str, error.absolute_path)) or "<root>"
        raise WorkspaceError(f"backup manifest validation failed at {location}: {error.message}")
    unsigned = {key: item for key, item in value.items() if key != "commit_id"}
    expected = "backup-commit-" + hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if value["commit_id"] != expected:
        raise WorkspaceError("backup commit identity does not match its manifest")


def _pointer_generation(results: Path, store: str) -> tuple[dict[str, Any], list[Path]]:
    prefix = Path() if store == "sources" else Path("source-notes" if store == "notes" else "learning")
    pointer = results / prefix / "current.json"
    if not pointer.is_file():
        return {"store": store, "commit_id": None, "revision": 0, "schema_version": None}, []
    selected = read_json(pointer)
    commit_id = selected.get("commit_id")
    manifest = results / prefix / "commits" / f"{commit_id}.json"
    if not manifest.is_file() or _digest(manifest) != selected.get("manifest_sha256"):
        raise WorkspaceError(f"{store} current pointer is incomplete or corrupt")
    value = read_json(manifest)
    objects_root = results / prefix / "objects" if store != "sources" else results / "objects"
    objects = []
    for digest, facts in sorted(value.get("objects", {}).items()):
        path = objects_root / digest[:2] / digest
        if not path.is_file() or path.stat().st_size != facts.get("size") or _digest(path) != digest:
            raise WorkspaceError(f"{store} object is incomplete or corrupt: {digest}")
        objects.append(path)
    return {"store": store, "commit_id": commit_id, "revision": value.get("revision"),
            "schema_version": value.get("schema_version")}, [pointer, manifest, *objects]


def _receipt_files(results: Path) -> list[Path]:
    root = results / "operation-receipts"
    if not root.exists():
        return []
    if root.is_symlink():
        raise WorkspaceError("operation receipt root must not be a symbolic link")
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise WorkspaceError(f"operation receipt must not be a symbolic link: {path}")
        if path.is_file():
            if path.suffix != ".json" or path.name.startswith("."):
                raise WorkspaceError(f"unexpected formal receipt file: {path}")
            value = read_json(path)
            required = {"schema_version", "operation_id", "status", "authoritative_revision",
                        "authoritative_digest", "intent", "artifact_facts", "attempts",
                        "reconciliation", "receipt"}
            if not isinstance(value, dict) or set(value) != required \
                    or value.get("schema_version") != 1 \
                    or not isinstance(value.get("operation_id"), str) \
                    or not isinstance(value.get("authoritative_revision"), int) \
                    or value["authoritative_revision"] < 1 \
                    or not isinstance(value.get("authoritative_digest"), str) \
                    or len(value["authoritative_digest"]) != 64 \
                    or any(character not in "0123456789abcdef" for character in value["authoritative_digest"]) \
                    or not isinstance(value.get("intent"), dict) \
                    or not isinstance(value.get("artifact_facts"), dict) \
                    or not isinstance(value.get("attempts"), list) \
                    or not isinstance(value.get("reconciliation"), list) \
                    or not isinstance(value.get("receipt"), dict) \
                    or not isinstance(value["receipt"].get("state"), str):
                raise WorkspaceError(f"invalid operation receipt: {path}")
            forbidden = {"token", "authorization", "cookie", "password", "secret", "credential"}
            def check(item: Any) -> None:
                if isinstance(item, dict):
                    for key, child in item.items():
                        if key.lower() in forbidden:
                            raise WorkspaceError(f"private field is forbidden in operation receipt: {key}")
                        check(child)
                elif isinstance(item, list):
                    for child in item: check(child)
            check(value); files.append(path)
    return files


def _entry(source: Path, destination: Path, payload: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    with destination.open("rb") as stream:
        os.fsync(stream.fileno())
    return {"path": destination.relative_to(payload.parent).as_posix(),
            "size": destination.stat().st_size, "sha256": _digest(destination)}


def _root_digest(entries: list[dict[str, Any]]) -> str:
    value = [{key: item[key] for key in ("path", "size", "sha256")} for item in sorted(entries, key=lambda x: x["path"])]
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _deep_validate_payload(root: Path, manifest: dict[str, Any]) -> None:
    payload = root / "payload"
    config = WorkspaceConfig(Path("restored/workspace.toml"), "backup-verification", root,
        root / "project", root / "sources", root / "derived", root / "derived/generated",
        root / "derived/threads", root / "derived/concepts", root / "derived/REVIEW.md",
        manifest["workspace_id"], schema_version=PORTABLE_SCHEMA_VERSION,
        results=payload, sources=root / "sources", derived=root / "derived", local=root / "local")
    from .source_registry import _load_snapshot, _validate_snapshot_objects
    from .authoritative_notes import _load_current
    from .learning import _load
    source = _load_snapshot(config)
    _validate_snapshot_objects(config, source)
    notes = _load_current(config)
    learning = _load(config)
    expected = manifest["authorities"]
    observed = {"sources": source, "notes": notes, "learning": learning}
    for name, value in observed.items():
        authority = expected[name]
        if authority["commit_id"] != value.get("commit_id") or authority["revision"] != value.get("revision"):
            raise WorkspaceError(f"{name} authority does not match the pinned backup generation")
    # Foreign keys remain tied to the pinned source generation even when its external location is absent.
    versions = {(source_id, version_id) for source_id, package in source.get("sources", {}).items()
                for version_id in package.get("versions", {})}
    for note in notes.get("notes", {}).values():
        for history in note.get("history", []):
            if (note["source_id"], history["source_version"]) not in versions:
                raise WorkspaceError("note history has a broken source-version association")
    for module in learning.get("record", {}).get("modules", {}).values():
        for ref in module.get("source_refs", []):
            if (ref["source_id"], ref["source_version"]) not in versions:
                raise WorkspaceError("learning record has a broken source-version association")
    _receipt_files(payload)


def create_backup(config: WorkspaceConfig, target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve(strict=False)
    if config.schema_version != PORTABLE_SCHEMA_VERSION or config.results is None:
        return response(status="unsupported", workspace=str(config.config_path),
                        validation={"workspace_schema": "failed"}, diagnostics=["backup requires workspace schema v2"])
    if target.exists():
        return response(status="awaiting_user", workspace=str(config.config_path),
                        validation={"target": "conflict"}, diagnostics=[f"backup target already exists: {target}"])
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent))
    try:
        with package_lock(config.results):
            before = {name: _pointer_generation(config.results, name) for name in ("sources", "notes", "learning")}
            payload = stage / "payload"; entries: list[dict[str, Any]] = []
            authorities: dict[str, Any] = {}
            for name, (authority, paths) in before.items():
                if authority["commit_id"] is None:
                    raise WorkspaceError(f"cannot create a complete backup without a published {name} generation")
                store_entries = []
                for source in paths:
                    relative = source.relative_to(config.results)
                    item = _entry(source, payload / relative, payload)
                    entries.append(item); store_entries.append(item)
                authorities[name] = {**authority, "root_sha256": _root_digest(store_entries)}
            receipt_entries = []
            for source in _receipt_files(config.results):
                item = _entry(source, payload / source.relative_to(config.results), payload)
                entries.append(item); receipt_entries.append(item)
            authorities["operation_receipts"] = {"store": "operation_receipts", "commit_id": None,
                "revision": len(receipt_entries), "schema_version": 1,
                "root_sha256": _root_digest(receipt_entries)}
            after = {name: _pointer_generation(config.results, name)[0] for name in ("sources", "notes", "learning")}
            if any(before[name][0] != after[name] for name in after):
                raise WorkspaceError("an authoritative generation changed while backup was being prepared")
        unsigned = {"schema_version": SCHEMA_VERSION, "workspace_id": config.workspace_id,
            "created_at": _now(), "tool_version": engineering_revision(), "authorities": authorities,
            "entries": sorted(entries, key=lambda item: item["path"]),
            "exclusions": ["local", "locks", "credentials", "temporary_files", "candidates", "derived_data", "source_media"]}
        manifest = {**unsigned, "commit_id": "backup-commit-" + hashlib.sha256(
            json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
        _validate_manifest(manifest)
        atomic_write_json(stage / MANIFEST, manifest)
        verified = verify_backup(stage)
        if verified["status"] != "completed":
            raise WorkspaceError("staged backup failed deep verification: " + "; ".join(verified["diagnostics"]))
        if os.environ.get("VIDEO_EXTRACT_BACKUP_TEST_FAULT") == "before_publish":
            raise OSError("injected backup interruption before publish")
        os.replace(stage, target)
        descriptor = os.open(target.parent, os.O_RDONLY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
        return response(status="completed", workspace=str(config.config_path), operation_id="operation-" + str(uuid.uuid4()),
            result={"backup": str(target), "commit_id": manifest["commit_id"], "entries": len(entries)},
            validation={"manifest": "passed", "digests": "passed", "associations": "passed"},
            provenance={"tool_version": manifest["tool_version"], "authority_revisions": {
                key: value["revision"] for key, value in authorities.items()}})
    except PackageBusyError as exc:
        return response(status="busy", workspace=str(config.config_path), diagnostics=[str(exc)])
    except (OSError, ValueError, WorkspaceError, json.JSONDecodeError) as exc:
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        validation={"published": "no"}, diagnostics=[str(exc)])
    finally:
        if stage.exists(): shutil.rmtree(stage)


def verify_backup(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve(strict=False)
    validation = {"manifest": "failed", "digests": "not_checked", "associations": "not_checked"}
    try:
        if root.is_symlink() or not root.is_dir():
            raise WorkspaceError(f"backup directory is missing or symbolic: {root}")
        manifest_path = root / MANIFEST
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise WorkspaceError("backup manifest is missing or symbolic")
        manifest = read_json(manifest_path); _validate_manifest(manifest); validation["manifest"] = "passed"
        listed_paths = [item["path"] for item in manifest["entries"]]
        if len(listed_paths) != len(set(listed_paths)):
            raise WorkspaceError("backup manifest contains duplicate paths")
        actual_paths = set()
        for item in manifest["entries"]:
            path = _safe(root, item["path"]); actual_paths.add(item["path"])
            if not path.is_file() or path.stat().st_size != item["size"] or _digest(path) != item["sha256"]:
                validation["digests"] = "failed"
                raise WorkspaceError(f"backup entry is missing or corrupt: {item['path']}")
        payload_nodes = list((root / "payload").rglob("*"))
        if any(path.is_symlink() for path in payload_nodes):
            raise WorkspaceError("backup payload contains a symbolic link")
        on_disk = {path.relative_to(root).as_posix() for path in payload_nodes if path.is_file()}
        if on_disk != actual_paths:
            validation["digests"] = "failed"
            raise WorkspaceError("backup contains unlisted or missing payload files")
        validation["digests"] = "passed"
        for name, authority in manifest["authorities"].items():
            selected = [item for item in manifest["entries"] if (
                item["path"].startswith("payload/operation-receipts/") if name == "operation_receipts" else
                item["path"].startswith("payload/source-notes/") if name == "notes" else
                item["path"].startswith("payload/learning/") if name == "learning" else
                item["path"].startswith("payload/objects/") or item["path"].startswith("payload/commits/") or item["path"] == "payload/current.json")]
            if _root_digest(selected) != authority["root_sha256"]:
                raise WorkspaceError(f"{name} authority root digest mismatch")
        _deep_validate_payload(root, manifest); validation["associations"] = "passed"
        return response(status="completed", result={"backup": str(root), "commit_id": manifest["commit_id"],
            "workspace_id": manifest["workspace_id"], "entries": len(manifest["entries"])}, validation=validation,
            provenance={"tool_version": manifest["tool_version"]})
    except (OSError, ValueError, WorkspaceError, json.JSONDecodeError) as exc:
        return response(status="failed", result={"backup": str(root)}, validation=validation, diagnostics=[str(exc)])


def restore_backup(root: Path, config: WorkspaceConfig) -> dict[str, Any]:
    checked = verify_backup(root)
    if checked["status"] != "completed":
        return response(status="failed", workspace=str(config.config_path), validation=checked["validation"],
                        diagnostics=checked["diagnostics"])
    if config.schema_version != PORTABLE_SCHEMA_VERSION or config.results is None:
        return response(status="unsupported", workspace=str(config.config_path), diagnostics=["restore requires workspace schema v2"])
    target = config.results.resolve(strict=False)
    if target.exists():
        return response(status="awaiting_user", workspace=str(config.config_path),
                        validation={"target": "conflict"}, diagnostics=[f"restore target already exists: {target}"],
                        next_action={"type": "user", "reason": "choose an empty isolated results root; existing data was preserved"})
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
    try:
        manifest = read_json(root / MANIFEST)
        for item in manifest["entries"]:
            source = _safe(root, item["path"])
            relative = Path(item["path"]).relative_to("payload")
            destination = _safe(stage, relative.as_posix())
            destination.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(source, destination)
        atomic_write_json(stage / "recovery-state.json", {"schema_version": 1,
            "backup_commit_id": manifest["commit_id"], "restored_at": _now(),
            "delivery": "paused", "external_operations": "paused", "unique_host_verified": False})
        # Validate the exact authoritative files before making the new results root visible.
        synthetic = replace(config, results=stage)
        from .source_registry import _load_snapshot, _validate_snapshot_objects
        from .authoritative_notes import _load_current
        from .learning import _load
        source = _load_snapshot(synthetic); _validate_snapshot_objects(synthetic, source)
        _load_current(synthetic); _load(synthetic); _receipt_files(stage)
        if os.environ.get("VIDEO_EXTRACT_BACKUP_TEST_FAULT") == "restore_before_publish":
            raise OSError("injected restore interruption before publish")
        os.replace(stage, target)
        descriptor = os.open(target.parent, os.O_RDONLY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
        missing = []
        for source_id, package in source.get("sources", {}).items():
            version = package["versions"][package["current_version"]]
            if version.get("storage") == "reference": missing.append({"source_id": source_id,
                "source_version": version["source_version"], "availability": "missing_external_source"})
        return response(status="completed", workspace=str(config.config_path), operation_id="operation-" + str(uuid.uuid4()),
            result={"results": str(target), "backup_commit_id": manifest["commit_id"],
                    "source_availability": missing, "delivery": "paused", "external_operations": "paused"},
            validation={"backup": "passed", "authorities": "passed", "target": "new_isolated_root"},
            next_action={"type": "user", "reason": "verify a unique delivery host before enabling reminders or external operations"})
    except (OSError, ValueError, WorkspaceError, json.JSONDecodeError) as exc:
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        validation={"published": "no"}, diagnostics=[str(exc)])
    finally:
        if stage.exists(): shutil.rmtree(stage)
