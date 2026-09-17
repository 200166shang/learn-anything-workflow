"""Scoped media acquisition with durable, workspace-local operation state."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .command_response import response
from .manifest import atomic_write_json, read_json
from .media_request import MediaKind, MediaRequest
from .source_registry import verify
from .workspace import WorkspaceConfig, WorkspaceError, discover_workspace


CONTRACT_VERSION = 1
KINDS = ("video", "audio", "subtitles")


def _normalized(request: dict[str, Any]) -> dict[str, Any]:
    scope = request.get("scope")
    media = request.get("media")
    if not isinstance(scope, list) or not scope or not all(isinstance(x, str) and x for x in scope):
        raise ValueError("scope must be a non-empty list of item ids")
    if not isinstance(media, list) or not media or any(x not in KINDS for x in media):
        raise ValueError("media must contain video, audio, or subtitles")
    if scope != sorted(set(scope)) or media != sorted(set(media)):
        raise ValueError("scope and media must be sorted and contain no duplicates")
    language = request.get("language", "original")
    quality = request.get("quality", "high")
    if not isinstance(language, str) or not language.strip():
        raise ValueError("language cannot be empty")
    if quality not in {"standard", "balanced", "high"}:
        raise ValueError("unsupported quality")
    for key in ("source_id", "source_version"):
        if not isinstance(request.get(key), str) or not request[key]:
            raise ValueError(f"{key} is required")
    return {"contract_version": CONTRACT_VERSION,
            "source_id": request["source_id"], "source_version": request["source_version"],
            "scope": sorted(set(scope)), "media": sorted(set(media)),
            "language": language, "quality": quality}


def operation_identity(request: dict[str, Any]) -> str:
    # Use the stable capability seam so direct ensure and capability dispatch
    # converge on one operation identity.
    from .capabilities import CAPABILITIES, _operation_id
    _normalized(request)  # reject malformed identity inputs before hashing
    return _operation_id(CAPABILITIES["media.acquire"], request)


def _root(config: WorkspaceConfig) -> Path:
    if config.local is None:
        raise WorkspaceError("media operations require workspace schema v2")
    root = config.local / "operations"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path(config: WorkspaceConfig, operation_id: str) -> Path:
    if not operation_id.startswith("operation-") or not operation_id.removeprefix("operation-").isalnum():
        raise ValueError("invalid operation_id")
    return _root(config) / f"{operation_id}.json"


@contextmanager
def _lock(config: WorkspaceConfig, operation_id: str):
    path = _root(config) / f"{operation_id}.lock"
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _catalog(config: WorkspaceConfig, normalized: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    checked = verify(config, normalized["source_id"], normalized["source_version"])
    if checked["status"] != "completed":
        raise ValueError("registered source version is unavailable")
    found = checked["result"]
    if found.get("content") is not None:
        raw = json.loads(found["content"])
    elif found.get("location"):
        raw = read_json(Path(found["location"]))
    else:
        raise ValueError("registered source inventory is unavailable")
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise ValueError("source inventory must contain items")
    by_id = {str(item.get("id")): item for item in items if isinstance(item, dict) and item.get("id")}
    missing = [item_id for item_id in normalized["scope"] if item_id not in by_id]
    if missing:
        raise ValueError("scope contains unknown item ids: " + ", ".join(missing))
    selected = [by_id[item_id] for item_id in normalized["scope"]]
    if any(not isinstance(item.get("source"), str) or not item["source"] for item in selected):
        raise ValueError("selected inventory item lacks source")
    return selected, found


def plan_request(request: dict[str, Any]) -> dict[str, Any]:
    config = discover_workspace(Path(request["workspace"]))
    normalized = _normalized(request)
    items, source = _catalog(config, normalized)
    operation_id = operation_identity(request)
    result = {"items": [{"item_id": item["id"], "title": item.get("title")} for item in items],
              "effective_parameters": {key: normalized[key] for key in ("language", "media", "quality")},
              "source_id": normalized["source_id"], "source_version": normalized["source_version"]}
    return response(status="completed", workspace=str(config.config_path), operation_id=operation_id,
                    result=result, validation={"source_version": "passed", "scope": "passed"},
                    provenance={"source_version": normalized["source_version"],
                                "source_revision": source["revision"], "capability_contract_version": CONTRACT_VERSION})


def _target(config: WorkspaceConfig, operation_id: str, item_id: str) -> Path:
    assert config.results is not None
    safe = hashlib.sha256(item_id.encode()).hexdigest()[:16]
    return config.results / "media" / operation_id / safe


def _materialize_item(item: dict[str, Any], kinds: list[str], target: Path, *, language: str, quality: str) -> dict[str, Path]:
    from .media_workflow import ensure
    request = MediaRequest(tuple(MediaKind(kind) for kind in kinds), language, quality)
    result = ensure(item["source"], request, target)
    if result["status"] != "complete":
        raise RuntimeError("media adapter did not produce verified artifacts")
    manifest = read_json(target / "manifest.json")
    keys = {"video": "source_video", "audio": "source_audio", "subtitles": "source_subtitle"}
    return {kind: target / manifest["artifacts"][keys[kind]] for kind in kinds if keys[kind] in manifest.get("artifacts", {})}


def _absolute_refs(config: WorkspaceConfig, record: dict[str, Any]) -> list[str]:
    assert config.results is not None
    return [str(config.results / raw) for raw in record.get("artifacts", {}).values()]


def _missing(config: WorkspaceConfig, record: dict[str, Any], item_id: str, kinds: list[str]) -> list[str]:
    assert config.results is not None
    missing = []
    for kind in kinds:
        key = f"{item_id}:{kind}"
        path = config.results / record.get("artifacts", {}).get(key, "missing")
        fact = record.get("artifact_facts", {}).get(key, {})
        if not path.is_file() or fact.get("sha256") != _sha256(path) or fact.get("size") != path.stat().st_size:
            missing.append(kind)
    return missing


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _public(config: WorkspaceConfig, record: dict[str, Any], reuse: str | None = None) -> dict[str, Any]:
    result = {"progress": record.get("progress", {}), "reuse": reuse,
              "source_id": record["request"]["source_id"], "source_version": record["request"]["source_version"]}
    public_status = "busy" if record["status"] == "running" else record["status"]
    return response(status=public_status, workspace=str(config.config_path), operation_id=record["operation_id"],
                    result=result, artifact_refs=_absolute_refs(config, record),
                    validation=record.get("validation", {}), provenance=record.get("provenance", {}),
                    next_action=record.get("next_action"), diagnostics=record.get("diagnostics", []))


def ensure_request(request: dict[str, Any], *, resume: bool = False) -> dict[str, Any]:
    config = discover_workspace(Path(request["workspace"]))
    normalized = _normalized(request)
    operation_id = operation_identity(request)
    path = _path(config, operation_id)
    with _lock(config, operation_id) as acquired:
        if not acquired:
            record = read_json(path) if path.is_file() else {"operation_id": operation_id, "request": normalized,
                                                              "status": "busy", "artifacts": {}}
            record["status"] = "busy"
            return _public(config, record)
        record = read_json(path) if path.is_file() else None
        if record and record.get("status") == "running" and not resume:
            record["status"] = "busy"
            return _public(config, record)
        items, source = _catalog(config, normalized)
        if record is None:
            record = {"schema_version": 1, "operation_id": operation_id, "request": normalized,
                      "status": "running", "artifacts": {}, "artifact_facts": {}, "progress": {}}
        missing_any = any(_missing(config, record, str(item["id"]), normalized["media"]) for item in items)
        if record.get("status") == "completed" and not missing_any:
            return _public(config, record, "verified_operation")
        record["status"] = "running"
        record["lease"] = {"pid": os.getpid()}
        atomic_write_json(path, record)
        try:
            for item in items:
                item_id = str(item["id"])
                kinds = _missing(config, record, item_id, normalized["media"])
                if not kinds:
                    continue
                outputs = _materialize_item(item, kinds, _target(config, operation_id, item_id),
                                            language=normalized["language"], quality=normalized["quality"])
                assert config.results is not None
                for kind, artifact in outputs.items():
                    key = f"{item_id}:{kind}"
                    record["artifacts"][key] = artifact.relative_to(config.results).as_posix()
                    record.setdefault("artifact_facts", {})[key] = {
                        "sha256": _sha256(artifact), "size": artifact.stat().st_size,
                        "validation": "content_digest_and_size"
                    }
                record["progress"][item_id] = {"completed": sorted(set(normalized["media"]) - set(_missing(config, record, item_id, normalized["media"]))) }
                atomic_write_json(path, record)
            remaining = {str(item["id"]): _missing(config, record, str(item["id"]), normalized["media"]) for item in items}
            remaining = {key: value for key, value in remaining.items() if value}
            record["status"] = "completed" if not remaining else "recoverable_failure"
            record["validation"] = {"source_version": "passed", "artifacts": "passed" if not remaining else "failed"}
            record["provenance"] = {"capability_id": "media.acquire", "implementation_version": 1,
                                    "source_version": normalized["source_version"], "source_revision": source["revision"],
                                    "capability_contract_version": CONTRACT_VERSION,
                                    "effective_parameters": {key: normalized[key] for key in ("scope", "media", "language", "quality")}}
            record.pop("lease", None)
            atomic_write_json(path, record)
            return _public(config, record)
        except Exception as exc:
            record["status"] = "recoverable_failure"
            record["diagnostics"] = [str(exc)]
            record["next_action"] = {"type": "resume", "operation_id": operation_id}
            record.pop("lease", None)
            atomic_write_json(path, record)
            return _public(config, record)


def show_operation(config: WorkspaceConfig, operation_id: str) -> dict[str, Any]:
    path = _path(config, operation_id)
    if not path.is_file():
        return response(status="missing_input", workspace=str(config.config_path), operation_id=operation_id,
                        validation={"operation": "failed"}, diagnostics=["unknown operation_id"])
    record = read_json(path)
    if record.get("status") == "completed":
        normalized = record["request"]
        items, _ = _catalog(config, normalized)
        if any(_missing(config, record, str(item["id"]), normalized["media"]) for item in items):
            record["status"] = "recoverable_failure"
            record["validation"] = {**record.get("validation", {}), "artifacts": "failed"}
            record["next_action"] = {"type": "resume", "operation_id": operation_id}
    return _public(config, record)


def resume_operation(config: WorkspaceConfig, operation_id: str) -> dict[str, Any]:
    path = _path(config, operation_id)
    if not path.is_file():
        return show_operation(config, operation_id)
    request = dict(read_json(path)["request"])
    request["workspace"] = str(config.config_path)
    return ensure_request(request, resume=True)


def write_test_lease(config: WorkspaceConfig, operation_id: str, request: dict[str, Any]) -> None:
    """Create the same durable state left by a live worker; used by isolated fake-adapter tests."""
    atomic_write_json(_path(config, operation_id), {"schema_version": 1, "operation_id": operation_id,
                      "request": _normalized(request), "status": "running", "artifacts": {}, "artifact_facts": {}, "progress": {},
                      "lease": {"pid": os.getpid()}})


def run_media_acquire(request: dict[str, Any]) -> dict[str, Any]:
    return ensure_request(request)


run_media_acquire.__capability_contract__ = {
    "input_type": "media-acquire-request-v1", "output_type": "command-response-v1"
}
