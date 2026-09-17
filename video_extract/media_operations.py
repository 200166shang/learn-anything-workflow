"""Scoped media acquisition with durable, workspace-local operation state."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .command_response import response
from .manifest import atomic_write_json, read_json
from .media_request import MediaKind, MediaRequest
from .source_registry import verify
from .workspace import WorkspaceConfig, WorkspaceError, discover_workspace


CONTRACT_VERSION = 1
KINDS = ("video", "audio", "subtitles")
SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "schemas" / "media-acquire-request-v1.schema.json").read_text(encoding="utf-8"))
LEASE_SECONDS = 30


class MediaAdapter(Protocol):
    adapter_identity: str
    authorization_category: str
    requires_authorization: bool

    def acquire(self, item: dict[str, Any], kinds: list[str], target: Path, *, language: str,
                quality: str, idempotency_token: str) -> dict[str, Any]: ...

    def reconcile(self, attempt: dict[str, Any], target: Path) -> dict[str, Any]: ...


class BuiltinMediaAdapter:
    """Read-only media adapter with a durable local receipt query protocol."""

    adapter_identity = "builtin.media-v1/local-read-v1"
    authorization_category = "user_authorized_media_source"
    requires_authorization = False

    def acquire(self, item: dict[str, Any], kinds: list[str], target: Path, *, language: str,
                quality: str, idempotency_token: str) -> dict[str, Any]:
        outputs = _materialize_item(item, kinds, target, language=language, quality=quality,
                                    idempotency_token=idempotency_token)
        receipt = target / ".adapter-receipts" / f"{idempotency_token}.json"
        atomic_write_json(receipt, {"idempotency_token": idempotency_token,
                                   "artifacts": {kind: path.relative_to(target).as_posix()
                                                 for kind, path in outputs.items()}})
        return {"artifacts": outputs, "query_handle": idempotency_token}

    def reconcile(self, attempt: dict[str, Any], target: Path) -> dict[str, Any]:
        token = attempt["idempotency_token"]
        receipt = target / ".adapter-receipts" / f"{token}.json"
        if not receipt.is_file():
            return {"state": "retry_safe", "query_handle": token,
                    "evidence": "adapter is read-only and no durable local completion receipt exists"}
        saved = read_json(receipt)
        if saved.get("idempotency_token") != token:
            return {"state": "unsupported", "evidence": "adapter receipt identity mismatch"}
        artifacts = {kind: target / raw for kind, raw in saved.get("artifacts", {}).items()}
        return {"state": "available", "query_handle": token, "artifacts": artifacts,
                "evidence": "durable adapter receipt"}


MEDIA_ADAPTERS: dict[str, MediaAdapter] = {"builtin.media-v1": BuiltinMediaAdapter()}


def register_media_adapter(adapter_id: str, adapter: MediaAdapter) -> None:
    """Register an adapter implementation; primarily an integration/test seam."""
    MEDIA_ADAPTERS[adapter_id] = adapter


def _adapter(item: dict[str, Any]) -> tuple[str, MediaAdapter | None]:
    adapter_id = str(item.get("adapter") or "builtin.media-v1")
    return adapter_id, MEDIA_ADAPTERS.get(adapter_id)


def _adapter_identity(adapter_id: str, adapter: MediaAdapter | None) -> str | None:
    if adapter is None:
        return adapter_id
    value = getattr(adapter, "adapter_identity", adapter_id)
    return value if isinstance(value, str) and value else adapter_id


def _validate_request(request: Any) -> dict[str, Any]:
    try:
        Draft202012Validator(SCHEMA).validate(request)
    except ValidationError as exc:
        location = "/".join(map(str, exc.absolute_path)) or "<root>"
        raise ValueError(f"media-acquire-request-v1.schema.json validation failed at {location}: {exc.message}") from exc
    assert isinstance(request, dict)
    return request


def _normalized(request: dict[str, Any]) -> dict[str, Any]:
    _validate_request(request)
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
    normalized = {"contract_version": CONTRACT_VERSION,
            "source_id": request["source_id"], "source_version": request["source_version"],
            "scope": sorted(set(scope)), "media": sorted(set(media)),
            "language": language, "quality": quality}
    if "authorization_ref" in request:
        normalized["authorization_ref"] = request["authorization_ref"]
    return normalized


def operation_identity(request: dict[str, Any]) -> str:
    # Use the stable capability seam so direct ensure and capability dispatch
    # converge on one operation identity.
    from .capabilities import CAPABILITIES, _operation_id
    _normalized(request)  # reject malformed identity inputs before hashing
    return _operation_id(CAPABILITIES["media.acquire"], request)


def _root(config: WorkspaceConfig, *, create: bool) -> Path:
    if config.local is None:
        raise WorkspaceError("media operations require workspace schema v2")
    root = config.local / "operations"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _path(config: WorkspaceConfig, operation_id: str, *, create_root: bool = False) -> Path:
    if not operation_id.startswith("operation-") or not operation_id.removeprefix("operation-").isalnum():
        raise ValueError("invalid operation_id")
    return _root(config, create=create_root) / f"{operation_id}.json"


@contextmanager
def _lock(config: WorkspaceConfig, operation_id: str):
    path = _root(config, create=True) / f"{operation_id}.lock"
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


def _materialize_item(item: dict[str, Any], kinds: list[str], target: Path, *, language: str, quality: str,
                      idempotency_token: str) -> dict[str, Path]:
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


def _owns_lease(path: Path, owner: str, fencing: int) -> bool:
    if not path.is_file():
        return False
    current = read_json(path)
    lease = current.get("lease", {})
    return current.get("status") == "running" and lease.get("owner") == owner and lease.get("fencing") == fencing


def _record_outputs(config: WorkspaceConfig, record: dict[str, Any], item_id: str,
                    outputs: dict[str, Path]) -> None:
    assert config.results is not None
    for kind, artifact in outputs.items():
        key = f"{item_id}:{kind}"
        record["artifacts"][key] = artifact.relative_to(config.results).as_posix()
        record.setdefault("artifact_facts", {})[key] = {
            "sha256": _sha256(artifact), "size": artifact.stat().st_size,
            "validation": "content_digest_and_size"
        }


def _intent(items: list[dict[str, Any]], normalized: dict[str, Any]) -> dict[str, Any]:
    identities: dict[str, str] = {}
    categories: set[str] = set()
    requires_authorization = False
    for item in items:
        adapter_id, adapter = _adapter(item)
        identity = _adapter_identity(adapter_id, adapter)
        if identity is not None:
            identities[str(item["id"])] = identity
        category = getattr(adapter, "authorization_category", "user_authorized_media_source") if adapter else "user_authorized_media_source"
        categories.add(str(category))
        requires_authorization = requires_authorization or bool(getattr(adapter, "requires_authorization", False))
    category_value = next(iter(categories)) if len(categories) == 1 else "+".join(sorted(categories))
    return {
        "capability_id": "media.acquire",
        "capability_contract_version": CONTRACT_VERSION,
        "adapter_identities": identities,
        "authorization_category": category_value,
        **({"authorization_ref": normalized["authorization_ref"]} if normalized.get("authorization_ref") else {}),
        "source_id": normalized["source_id"],
        "source_version": normalized["source_version"],
        "effective_parameters": {key: normalized[key] for key in ("scope", "media", "language", "quality")},
        "requires_authorization": requires_authorization,
    }


def _adapter_drift(record: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    expected = record.get("intent", {}).get("adapter_identities", {})
    drift = []
    for item in items:
        adapter_id, adapter = _adapter(item)
        actual = _adapter_identity(adapter_id, adapter)
        item_id = str(item["id"])
        if expected.get(item_id) != actual:
            drift.append(item_id)
    return drift


def _persist_adapter_drift(config: WorkspaceConfig, path: Path, record: dict[str, Any],
                           items: list[dict[str, Any]], *, next_type: str) -> dict[str, Any] | None:
    drift = _adapter_drift(record, items)
    if not drift:
        return None
    record["status"] = "uncertain"
    record["diagnostics"] = ["pinned adapter identity is unavailable or changed for: " + ", ".join(drift)]
    record["next_action"] = {
        "type": next_type,
        "reason": "restore an implementation for the pinned adapter identity; do not resubmit",
        **({"operation_id": record["operation_id"]} if next_type == "reconcile" else {}),
    }
    atomic_write_json(path, record)
    return _public(config, record)


def _write_result_receipt(config: WorkspaceConfig, record: dict[str, Any], receipt_state: str) -> None:
    assert config.results is not None
    projected_attempts = list(record.get("attempts", []))
    if isinstance(record.get("current_attempt"), dict):
        projected_attempts.append(record["current_attempt"])
    last_attempt = (projected_attempts or [{}])[-1]
    receipt = {
        "schema_version": 1,
        "operation_id": record["operation_id"],
        "status": record["status"],
        "authoritative_revision": record["commit"]["revision"],
        "authoritative_digest": record["commit"]["digest"],
        "intent": {key: value for key, value in record["intent"].items() if key != "requires_authorization"},
        "artifact_facts": record.get("artifact_facts", {}),
        "attempts": [{key: attempt.get(key) for key in
                      ("item_id", "media", "adapter_id", "idempotency_token", "query_handle", "result")}
                     for attempt in projected_attempts],
        "reconciliation": [{key: entry.get(key) for key in
                            ("state", "adapter_id", "query_handle", "queried_at", "receipt") if entry.get(key) is not None}
                           for entry in record.get("reconciliation", [])],
        "receipt": {"state": receipt_state, "recorded_at": _now().isoformat(),
                    "adapter_id": last_attempt.get("adapter_id"),
                    "idempotency_token": last_attempt.get("idempotency_token"),
                    "result": last_attempt.get("result")},
    }
    atomic_write_json(config.results / "operation-receipts" / f"{record['operation_id']}.json", receipt)


def _authority_digest(record: dict[str, Any]) -> str:
    authoritative = {key: value for key, value in record.items() if key != "commit"}
    encoded = json.dumps(authoritative, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _commit_authority(path: Path, record: dict[str, Any]) -> None:
    previous = record.get("commit", {}).get("revision", 0)
    record["commit"] = {"revision": int(previous) + 1, "committed_at": _now().isoformat()}
    record["commit"]["digest"] = _authority_digest(record)
    atomic_write_json(path, record)


def _project_receipt(config: WorkspaceConfig, record: dict[str, Any], state: str) -> str | None:
    try:
        _write_result_receipt(config, record, state)
    except OSError as exc:
        return str(exc)
    return None


def _public(config: WorkspaceConfig, record: dict[str, Any], reuse: str | None = None) -> dict[str, Any]:
    result = {"progress": record.get("progress", {}), "reuse": reuse,
              "source_id": record["request"]["source_id"], "source_version": record["request"]["source_version"]}
    public_status = record["status"]
    if public_status == "running":
        public_status = "busy" if _lease_active(record) else "uncertain"
    return response(status=public_status, workspace=str(config.config_path), operation_id=record["operation_id"],
                    result=result, artifact_refs=_absolute_refs(config, record),
                    validation=record.get("validation", {}), provenance=record.get("provenance", {}),
                    next_action=record.get("next_action"), diagnostics=record.get("diagnostics", []))


def ensure_request(request: dict[str, Any], *, resume: bool = False) -> dict[str, Any]:
    _validate_request(request)
    config = discover_workspace(Path(request["workspace"]))
    normalized = _normalized(request)
    operation_id = operation_identity(request)
    path = _path(config, operation_id, create_root=True)
    with _lock(config, operation_id) as acquired:
        if not acquired:
            record = read_json(path) if path.is_file() else {"operation_id": operation_id, "request": normalized,
                                                              "status": "busy", "artifacts": {}}
            record["status"] = "busy"
            return _public(config, record)
        record = read_json(path) if path.is_file() else None
        if record and record.get("status") == "running":
            if _lease_active(record):
                record["status"] = "busy"
                return _public(config, record)
            record["status"] = "uncertain"
            record["next_action"] = {"type": "reconcile", "operation_id": operation_id}
            record["diagnostics"] = ["previous worker lease expired before its adapter result was confirmed"]
            atomic_write_json(path, record)
            return _public(config, record)
        if record and record.get("status") == "uncertain":
            return _public(config, record)
        items, source = _catalog(config, normalized)
        if record is None:
            record = {"schema_version": 1, "operation_id": operation_id, "request": normalized,
                      "status": "prepared", "artifacts": {}, "artifact_facts": {}, "progress": {},
                      "intent": _intent(items, normalized)}
            atomic_write_json(path, record)
        elif (record.get("status") == "awaiting_user" and normalized.get("authorization_ref")
              and not record.get("intent", {}).get("authorization_ref")):
            record["request"]["authorization_ref"] = normalized["authorization_ref"]
            record["intent"]["authorization_ref"] = normalized["authorization_ref"]
            record["diagnostics"] = []
            record["next_action"] = None
            atomic_write_json(path, record)
        drift_response = _persist_adapter_drift(config, path, record, items, next_type="reconcile")
        if drift_response is not None:
            return drift_response
        if record["intent"].get("requires_authorization") and not record["intent"].get("authorization_ref"):
            record["status"] = "awaiting_user"
            record["diagnostics"] = [f"{record['intent']['authorization_category']} requires a non-sensitive authorization_ref"]
            record["next_action"] = {"type": "user", "reason": "provide an authorization reference; never place credentials in the request"}
            atomic_write_json(path, record)
            return _public(config, record)
        missing_any = any(_missing(config, record, str(item["id"]), normalized["media"]) for item in items)
        if record.get("status") == "completed" and not missing_any:
            if record.get("commit", {}).get("digest") != _authority_digest(record):
                record["status"] = "uncertain"
                record["validation"] = {**record.get("validation", {}), "authoritative_record": "failed"}
                record["diagnostics"] = ["completed operation authority digest is missing or invalid"]
                record["next_action"] = {"type": "maintenance", "reason": "verify the authoritative operation record"}
                return _public(config, record)
            _project_receipt(config, record, "verified_authoritative_record")
            return _public(config, record, "verified_operation")
        record["status"] = "running"
        fencing = int(record.get("fencing", 0)) + 1
        record["fencing"] = fencing
        record["lease"] = _new_lease(fencing)
        owner = record["lease"]["owner"]
        atomic_write_json(path, record)
        try:
            for item in items:
                record["lease"] = _new_lease(fencing, owner=record["lease"]["owner"])
                atomic_write_json(path, record)
                item_id = str(item["id"])
                kinds = _missing(config, record, item_id, normalized["media"])
                if not kinds:
                    continue
                attempt_token = hashlib.sha256(
                    f"{operation_id}:{item_id}:{','.join(normalized['media'])}".encode()
                ).hexdigest()
                adapter_id, adapter = _adapter(item)
                record["current_attempt"] = {"item_id": item_id, "media": kinds,
                                             "idempotency_token": attempt_token,
                                             "query_handle": attempt_token,
                                             "adapter_id": adapter_id, "fencing": fencing, "owner": owner}
                atomic_write_json(path, record)
                if adapter is None:
                    raise RuntimeError(f"media adapter reconciliation is unsupported: {adapter_id}")
                acquired_result = adapter.acquire(
                    item, kinds, _target(config, operation_id, item_id), language=normalized["language"],
                    quality=normalized["quality"], idempotency_token=attempt_token)
                if not _owns_lease(path, owner, fencing):
                    latest = read_json(path)
                    latest["diagnostics"] = ["stale fenced adapter result was rejected"]
                    return _public(config, latest)
                outputs = acquired_result.get("artifacts")
                if not isinstance(outputs, dict):
                    raise RuntimeError("media adapter returned no artifact mapping")
                record["current_attempt"]["query_handle"] = acquired_result.get("query_handle", attempt_token)
                _record_outputs(config, record, item_id, outputs)
                record["progress"][item_id] = {"completed": sorted(set(normalized["media"]) - set(_missing(config, record, item_id, normalized["media"]))) }
                record.setdefault("attempts", []).append({**record.pop("current_attempt"), "result": "verified_local"})
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
            if record["status"] == "completed":
                _commit_authority(path, record)
                projection_error = _project_receipt(config, record, "verified_adapter_result")
                public = _public(config, record)
                if projection_error:
                    public["validation"] = {**public["validation"], "operation_receipt": "not_synced"}
                    public["diagnostics"] = [f"operation receipt projection interrupted: {projection_error}"]
                return public
            atomic_write_json(path, record)
            return _public(config, record)
        except Exception as exc:
            if not _owns_lease(path, owner, fencing):
                latest = read_json(path)
                latest["diagnostics"] = ["stale fenced adapter failure was rejected"]
                return _public(config, latest)
            record["status"] = "uncertain"
            record["diagnostics"] = [str(exc)]
            record["next_action"] = {"type": "reconcile", "operation_id": operation_id}
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
        elif record.get("commit", {}).get("digest") != _authority_digest(record):
            record["status"] = "uncertain"
            record["validation"] = {**record.get("validation", {}), "authoritative_record": "failed"}
            record["diagnostics"] = ["completed operation authority digest is missing or invalid"]
            record["next_action"] = {"type": "maintenance", "reason": "verify the authoritative operation record before trusting its receipt"}
    return _public(config, record)


def resume_operation(config: WorkspaceConfig, operation_id: str) -> dict[str, Any]:
    path = _path(config, operation_id)
    if not path.is_file():
        return show_operation(config, operation_id)
    request = dict(read_json(path)["request"])
    request["workspace"] = str(config.config_path)
    return ensure_request(request, resume=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_lease(fencing: int, *, owner: str | None = None) -> dict[str, Any]:
    now = _now()
    return {"owner": owner or str(uuid.uuid4()), "pid": os.getpid(), "fencing": fencing,
            "heartbeat_at": now.isoformat(), "expires_at": (now + timedelta(seconds=LEASE_SECONDS)).isoformat()}


def _lease_active(record: dict[str, Any]) -> bool:
    lease = record.get("lease")
    if not isinstance(lease, dict) or not all(key in lease for key in ("owner", "fencing", "heartbeat_at", "expires_at")):
        return False
    try:
        return datetime.fromisoformat(lease["expires_at"]) > _now()
    except (TypeError, ValueError):
        return False


def _check_adapter_result(config: WorkspaceConfig, record: dict[str, Any]) -> dict[str, Any]:
    attempt = record.get("current_attempt")
    if not isinstance(attempt, dict):
        return {"state": "unsupported", "evidence": "operation has no queryable adapter attempt"}
    adapter_id = attempt.get("adapter_id")
    adapter = MEDIA_ADAPTERS.get(adapter_id)
    if adapter is None:
        return {"state": "unsupported", "adapter_id": adapter_id,
                "evidence": "adapter does not implement reconciliation"}
    target = _target(config, record["operation_id"], attempt["item_id"])
    result = adapter.reconcile(attempt, target)
    if result.get("state") not in {"not_submitted", "retry_safe", "committed", "available", "unsupported"}:
        return {"state": "unsupported", "adapter_id": adapter_id,
                "evidence": "adapter returned an invalid reconciliation state"}
    return {"adapter_id": adapter_id, **result}


def _safe_reconciliation(checked: dict[str, Any]) -> dict[str, Any]:
    receipt = checked.get("receipt")
    safe_receipt = None
    if isinstance(receipt, dict):
        facts = receipt.get("facts")
        safe_receipt = {
            key: receipt[key] for key in ("receipt_id", "ledger_digest")
            if isinstance(receipt.get(key), str) and receipt[key]
        }
        if isinstance(facts, dict):
            safe_receipt["facts"] = {
                str(key): value for key, value in facts.items()
                if isinstance(value, (str, int, float, bool)) or value is None
            }
    return {
        "state": checked.get("state"), "adapter_id": checked.get("adapter_id"),
        "query_handle": checked.get("query_handle"), "queried_at": _now().isoformat(),
        **({"receipt": safe_receipt} if safe_receipt else {}),
    }


def reconcile_operation(config: WorkspaceConfig, operation_id: str) -> dict[str, Any]:
    path = _path(config, operation_id)
    if not path.is_file():
        return show_operation(config, operation_id)
    with _lock(config, operation_id) as acquired:
        if not acquired:
            record = read_json(path); record["status"] = "busy"; return _public(config, record)
        record = read_json(path)
        items, _ = _catalog(config, record["request"])
        drift_response = _persist_adapter_drift(config, path, record, items, next_type="user")
        if drift_response is not None:
            return drift_response
        try:
            checked = _check_adapter_result(config, record)
        except Exception as exc:
            attempt = record.get("current_attempt", {})
            record["status"] = "uncertain"
            record.setdefault("reconciliation", []).append({
                "state": "query_failed", "adapter_id": attempt.get("adapter_id"),
                "query_handle": attempt.get("query_handle"), "queried_at": _now().isoformat(),
                "diagnostic": str(exc),
            })
            record["diagnostics"] = [f"adapter result query failed: {exc}"]
            command = (f"video-extract operation reconcile {shlex.quote(operation_id)} "
                       f"--workspace {shlex.quote(str(config.config_path))} --json")
            record["next_action"] = {"type": "reconcile", "command": command,
                                     "reason": "retry the result query; do not resume acquisition"}
            atomic_write_json(path, record)
            return _public(config, record)
        state = checked.get("state")
        record.setdefault("reconciliation", []).append(_safe_reconciliation(checked))
        if state in {"not_submitted", "retry_safe"}:
            record["status"] = "recoverable_failure"
            record["next_action"] = {"type": "resume", "operation_id": operation_id}
            record["diagnostics"] = []
        elif state == "available":
            attempt = record["current_attempt"]
            outputs = checked.get("artifacts")
            if not isinstance(outputs, dict) or not all(isinstance(path, Path) and path.is_file() for path in outputs.values()):
                record["status"] = "uncertain"
                record["next_action"] = {"type": "reconcile", "operation_id": operation_id,
                                         "reason": "adapter result is not locally verifiable"}
            else:
                _record_outputs(config, record, attempt["item_id"], outputs)
                record.setdefault("attempts", []).append({**record.pop("current_attempt"), "result": "reconciled_available"})
                items, _ = _catalog(config, record["request"])
                remaining = any(_missing(config, record, str(item["id"]), record["request"]["media"]) for item in items)
                record["status"] = "recoverable_failure" if remaining else "completed"
                record["next_action"] = ({"type": "resume", "operation_id": operation_id} if remaining else None)
                record["validation"] = {"source_version": "passed", "artifacts": "failed" if remaining else "passed"}
        elif state == "committed":
            record["status"] = "uncertain"
            record["next_action"] = {"type": "reconcile", "operation_id": operation_id,
                                     "reason": "committed adapter result must be queried/materialized, never resubmitted"}
        elif state == "unsupported":
            record["status"] = "uncertain"
            record["next_action"] = {"type": "user", "reason": "adapter result reconciliation is unsupported"}
        else:
            record["status"] = "uncertain"
            record["next_action"] = {"type": "reconcile", "operation_id": operation_id}
        record.pop("lease", None)
        # Reconciliation history is authoritative even when the effect is
        # absent or still unknown, and must survive result-only backup.
        _commit_authority(path, record)
        _project_receipt(config, record, "reconciled_available" if record["status"] == "completed" else f"reconciled_{state}")
        return _public(config, record)


def write_test_lease(config: WorkspaceConfig, operation_id: str, request: dict[str, Any], *, expires_at: str | None = None) -> None:
    """Create the same durable state left by a live worker; used by isolated fake-adapter tests."""
    lease = _new_lease(1)
    if expires_at:
        lease["expires_at"] = expires_at
    atomic_write_json(_path(config, operation_id, create_root=True), {"schema_version": 1, "operation_id": operation_id,
                      "request": _normalized(request), "status": "running", "fencing": 1,
                      "artifacts": {}, "artifact_facts": {}, "progress": {},
                      "lease": lease})


def run_media_acquire(request: dict[str, Any]) -> dict[str, Any]:
    return ensure_request(request)


run_media_acquire.__capability_contract__ = {
    "input_type": "media-acquire-request-v1", "output_type": "command-response-v1"
}
