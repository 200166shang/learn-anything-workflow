"""Authorized, recoverable publication of a verified Mandarin audio artifact."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .command_response import response
from .mandarin_audio import audio_info
from .manifest import atomic_write_json, read_json
from .netease_adapter import NETEASE_ADAPTERS, NetEaseAdapter, NcmCliAdapter, register_netease_adapter
from .netease_receipt import project as _project_receipts, valid as _receipts_valid
from .workspace import WorkspaceConfig, discover_workspace


CONTRACT_VERSION = 1
SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "schemas" /
                     "netease-publish-request-v1.schema.json").read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized(request: dict[str, Any]) -> dict[str, Any]:
    try:
        Draft202012Validator(SCHEMA).validate(request)
    except ValidationError as exc:
        raise ValueError("invalid NetEase publish request: " + exc.message) from exc
    return {**request, "adapter": request.get("adapter", "ncm-cli")}


def _inputs(config: WorkspaceConfig, request: dict[str, Any]) -> tuple[Path, Path, dict[str, Any], str | None, bool]:
    if config.results is None or config.local is None:
        raise ValueError("publish.netease requires workspace schema v2")
    package = Path(request["package"]).expanduser().resolve()
    manifest = read_json(package / "manifest.json")
    if manifest.get("schema_version") != 5 or not isinstance(manifest.get("identity"), str):
        raise ValueError("a canonical schema-v5 media package is required")
    title = manifest.get("title")
    reliable_title = (isinstance(title, str) and bool(title.strip())
                      and manifest.get("provenance", {}).get("title", {}).get("reliable") is True)
    output = package / "listening" / "zh-CN" / "podcast.zh-CN.mp3"
    if not output.is_file() or _sha256(output) != request["output_sha256"]:
        raise ValueError("verified Mandarin output digest does not match the canonical artifact")
    audio_receipt = config.results / "operation-receipts" / f'{request["audio_operation_id"]}.json'
    if not audio_receipt.is_file():
        raise ValueError("the referenced audio operation receipt is missing")
    receipt = read_json(audio_receipt)
    facts = receipt.get("artifact_facts", {})
    fact_digests = {value for fact in facts.values() if isinstance(fact, dict)
                    for key, value in fact.items() if key in {"sha256", "output_sha256"} and isinstance(value, str)}
    if isinstance(facts, dict) and isinstance(facts.get("output_sha256"), str):
        fact_digests.add(facts["output_sha256"])
    if receipt.get("status") != "completed" or request["output_sha256"] not in fact_digests:
        raise ValueError("the referenced audio operation does not authenticate this output digest")
    return package, output, manifest, title if isinstance(title, str) else None, reliable_title


def _operation_path(config: WorkspaceConfig, operation_id: str) -> Path:
    assert config.local is not None
    return config.local / "operations" / f"{operation_id}.json"


def operation_identity(request: dict[str, Any]) -> str:
    normalized = _normalized(request)
    config = discover_workspace(Path(normalized["workspace"]))
    _, _, manifest, _, _ = _inputs(config, normalized)
    logical = {"workspace_id": config.workspace_id, "capability_id": "publish.netease",
               "contract_version": CONTRACT_VERSION, "package_identity": manifest["identity"],
               "audio_operation_id": normalized["audio_operation_id"],
               "output_sha256": normalized["output_sha256"], "account_ref": normalized["account_ref"]}
    return "operation-" + hashlib.sha256(json.dumps(logical, sort_keys=True).encode()).hexdigest()[:24]


def _safe_filename(title: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip()
    if not value:
        raise ValueError("reliable original title has no filesystem-safe characters")
    return f"{value}.mp3"


def _public(config: WorkspaceConfig, record: dict[str, Any], reuse: str | None = None) -> dict[str, Any]:
    status = record["status"]
    validation = dict(record.get("validation", {}))
    diagnostics = list(record.get("diagnostics", []))
    next_action = record.get("next_action")
    if record.get("commit"):
        from .media_operations import _authority_digest
        if record["commit"].get("digest") != _authority_digest(record):
            status = "uncertain"
            validation["authoritative_record"] = "failed"
            diagnostics = ["authoritative operation digest is invalid"]
            next_action = {"type": "maintenance"}
    if status == "completed" and not _receipts_valid(record):
        status = "recoverable_failure"
        validation["remote_receipt"] = "failed"
        diagnostics = ["sanitized receipt projection is missing or invalid"]
        next_action = {"type": "reconcile", "operation_id": record["operation_id"],
                       "reason": "repair the receipt projection from authoritative state"}
    return response(status=status, workspace=str(config.config_path), operation_id=record["operation_id"],
                    result={"reuse": reuse, "filename": record["intent"]["filename"],
                            "remote_id": record.get("remote", {}).get("id")},
                    artifact_refs=record.get("artifact_refs", []), validation=validation,
                    provenance=record.get("provenance", {}), next_action=next_action,
                    diagnostics=diagnostics)


def _copy_for_upload(package: Path, source: Path, filename: str) -> Path:
    target = package / "publishing" / "netease" / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and _sha256(target) != _sha256(source):
        replacement = target.with_name(f".{target.name}.replacement")
        shutil.copy2(source, replacement)
        os.replace(replacement, target)
    elif not target.exists():
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
    return target


def _items(page: dict[str, Any]) -> list[dict[str, Any]]:
    raw = page.get("items") or page.get("songs") or page.get("data") or []
    if isinstance(raw, dict):
        raw = raw.get("items") or raw.get("songs") or raw.get("list") or []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _all_remote(adapter: NetEaseAdapter) -> list[dict[str, Any]]:
    cursor = None
    found = []
    seen = set()
    while True:
        page = adapter.list_page(cursor, 30)
        found.extend(_items(page))
        nested = page.get("data") if isinstance(page.get("data"), dict) else {}
        next_cursor = (page.get("next_cursor") or page.get("nextCursor") or
                       nested.get("next_cursor") or nested.get("nextCursor"))
        if not next_cursor or next_cursor in seen:
            return found
        seen.add(next_cursor)
        cursor = str(next_cursor)


def _duration_matches(remote: Any, local: float | None) -> bool:
    if local is None or not isinstance(remote, (int, float)) or isinstance(remote, bool):
        return False
    return abs(float(remote) - local) <= max(2.0, local * .01)


def _matches(items: list[dict[str, Any]], filename: str, digest: str,
             duration: float | None) -> list[dict[str, Any]]:
    named = [item for item in items if item.get("filename") == filename and item.get("visible") is True]
    exact = [item for item in named if item.get("sha256") == digest]
    approximate = [item for item in named if _duration_matches(item.get("duration"), duration)]
    return exact or approximate or named


def _identity(item: dict[str, Any], filename: str, digest: str) -> bool:
    return (item.get("visible") is True and item.get("filename") == filename
            and item.get("sha256") == digest
            and isinstance(item.get("id"), (str, int)))


def _finish(config: WorkspaceConfig, path: Path, record: dict[str, Any], package: Path,
            item: dict[str, Any], *, reuse: str) -> dict[str, Any]:
    if not _identity(item, record["intent"]["filename"], record["intent"]["output_sha256"]):
        record.update(status="uncertain", validation={"remote_object_identity": "failed"},
                      diagnostics=["remote object identity does not match the pinned audio digest"],
                      next_action={"type": "reconcile", "operation_id": record["operation_id"]})
        atomic_write_json(path, record)
        return _public(config, record)
    record.update(status="completed", remote={"id": str(item["id"]), "visible": True,
                                                                 "confirmed_at": datetime.now(timezone.utc).isoformat()},
                  validation={"upload_authority": "passed", "remote_receipt": "derived",
                              "remote_object_identity": "passed"}, diagnostics=[], next_action=None)
    assert config.results is not None
    package_receipt = package / "publishing" / "netease" / "receipt.json"
    result_receipt = config.results / "operation-receipts" / f'{record["operation_id"]}.json'
    record["artifact_refs"] = [str(package_receipt), str(result_receipt)]
    from .media_operations import _commit_authority
    _commit_authority(path, record)
    try:
        _project_receipts(record)
    except OSError as exc:
        public = _public(config, record, reuse)
        public["diagnostics"] = [f"receipt projection interrupted: {exc}"]
        return public
    return _public(config, record, reuse)


def run_netease_publish(request: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalized(request)
    config = discover_workspace(Path(normalized["workspace"]))
    package, output, manifest, title, reliable_title = _inputs(config, normalized)
    operation_id = operation_identity(normalized)
    path = _operation_path(config, operation_id)
    from .media_operations import _lock
    with _lock(config, operation_id) as acquired:
        if not acquired:
            return response(status="busy", workspace=str(config.config_path), operation_id=operation_id)
        record = read_json(path) if path.is_file() else {
            "schema_version": 1, "operation_id": operation_id, "status": "prepared",
            "request": {key: value for key, value in normalized.items() if key != "authorization_ref"},
            "intent": {"capability_id": "publish.netease", "capability_contract_version": CONTRACT_VERSION,
                       "adapter_identity": getattr(NETEASE_ADAPTERS.get(normalized["adapter"]), "adapter_identity", normalized["adapter"]),
                       "authorization_category": "netease_cloud_write", "source_identity": manifest["identity"],
                       "audio_operation_id": normalized["audio_operation_id"], "output_sha256": normalized["output_sha256"],
                       "account_ref": normalized["account_ref"],
                       "filename": _safe_filename(title) if reliable_title and title is not None else None,
                       "duration": audio_info(output).get("duration")},
            "provenance": {"audio_operation_id": normalized["audio_operation_id"], "source_version": manifest.get("source_version")},
        }
        atomic_write_json(path, record)
        if not reliable_title:
            record.update(status="awaiting_user", validation={"source_title": "failed"},
                          diagnostics=["a reliable original source title is required; do not translate or invent one"],
                          next_action={"type": "user", "reason": "provide and verify the original source title"})
            atomic_write_json(path, record)
            return _public(config, record)
        if record["intent"].get("filename") is None and title is not None:
            record["intent"]["filename"] = _safe_filename(title)
            atomic_write_json(path, record)
        if record["status"] == "completed":
            return _public(config, record, "verified_operation")
        if record["status"] == "running":
            record.update(status="uncertain", diagnostics=["previous upload worker ended before confirmation"],
                          next_action={"type": "reconcile", "operation_id": operation_id})
            record.pop("lease", None)
            atomic_write_json(path, record)
            return _public(config, record)
        if record["status"] == "uncertain":
            return _public(config, record)
        if normalized.get("authorization_ref") and not record["intent"].get("authorization_ref"):
            record["intent"]["authorization_ref"] = normalized["authorization_ref"]
            atomic_write_json(path, record)
        if not record["intent"].get("authorization_ref"):
            record.update(status="awaiting_user", validation={"authorization": "failed"},
                          diagnostics=["NetEase upload requires an explicit non-sensitive authorization_ref"],
                          next_action={"type": "user", "reason": "provide upload authorization"})
            atomic_write_json(path, record)
            return _public(config, record)
        adapter = NETEASE_ADAPTERS.get(normalized["adapter"])
        if adapter is None:
            record.update(status="missing_dependency", diagnostics=["configured NetEase adapter is unavailable"],
                          next_action={"type": "maintenance"})
            atomic_write_json(path, record)
            return _public(config, record)
        if getattr(adapter, "adapter_identity", None) != record["intent"]["adapter_identity"]:
            record.update(status="uncertain", diagnostics=["pinned NetEase adapter identity changed; do not upload"],
                          next_action={"type": "reconcile", "operation_id": operation_id})
            atomic_write_json(path, record)
            return _public(config, record)
        candidates = _matches(_all_remote(adapter), record["intent"]["filename"], normalized["output_sha256"],
                              record["intent"].get("duration"))
        if len(candidates) == 1 and _identity(candidates[0], record["intent"]["filename"], normalized["output_sha256"]):
            return _finish(config, path, record, package, candidates[0], reuse="verified_remote_object")
        if candidates:
            record.update(status="uncertain", validation={"remote_object_identity": "ambiguous"},
                          diagnostics=["same-name remote candidates cannot be uniquely verified"],
                          next_action={"type": "reconcile", "operation_id": operation_id})
            atomic_write_json(path, record)
            return _public(config, record)
        upload = _copy_for_upload(package, output, record["intent"]["filename"])
        if _sha256(upload) != normalized["output_sha256"]:
            record.update(status="recoverable_failure", validation={"upload_copy": "failed"},
                          diagnostics=["upload copy digest changed before adapter submission"],
                          next_action={"type": "resume", "operation_id": operation_id})
            atomic_write_json(path, record)
            return _public(config, record)
        token = hashlib.sha256(f"{operation_id}:upload".encode()).hexdigest()
        from .media_operations import _new_lease
        fencing = int(record.get("fencing", 0)) + 1
        lease = _new_lease(fencing)
        attempt = {"adapter_id": normalized["adapter"], "adapter_identity": record["intent"]["adapter_identity"],
                   "idempotency_token": token, "query_handle": token,
                   "filename": record["intent"]["filename"], "output_sha256": normalized["output_sha256"],
                   "duration": record["intent"].get("duration"),
                   "submitted_at": datetime.now(timezone.utc).isoformat(),
                   "owner": lease["owner"], "fencing": fencing}
        record["current_attempt"] = attempt
        record.update(status="running", fencing=fencing, lease=lease, next_action=None, diagnostics=[])
        atomic_write_json(path, record)
        try:
            submitted = adapter.upload(upload, idempotency_token=token)
            from .media_operations import _owns_lease
            if not _owns_lease(path, lease["owner"], fencing):
                latest = read_json(path)
                latest.update(status="uncertain", diagnostics=["stale fenced upload result was rejected"],
                              next_action={"type": "reconcile", "operation_id": operation_id})
                atomic_write_json(path, latest)
                return _public(config, latest)
            attempt["query_handle"] = str(submitted.get("query_handle") or token)
            upload_status = adapter.status(attempt["query_handle"])
            visible = _matches(_all_remote(adapter), record["intent"]["filename"], normalized["output_sha256"],
                               record["intent"].get("duration"))
        except Exception as exc:
            record.update(status="uncertain", diagnostics=[str(exc)],
                          next_action={"type": "reconcile", "operation_id": operation_id})
            record.pop("lease", None)
            from .media_operations import _commit_authority
            _commit_authority(path, record)
            return _public(config, record)
        record.pop("lease", None)
        if (upload_status.get("state") == "completed" and len(visible) == 1
                and _identity(visible[0], record["intent"]["filename"], normalized["output_sha256"])):
            return _finish(config, path, record, package, visible[0], reuse="submitted_and_verified")
        record.update(status="uncertain", validation={"remote_object_identity": "not_confirmed"},
                      diagnostics=["upload returned but the pinned object is not yet visibly confirmed"],
                      next_action={"type": "reconcile", "operation_id": operation_id})
        from .media_operations import _commit_authority
        _commit_authority(path, record)
        return _public(config, record)


run_netease_publish.__capability_contract__ = {
    "input_type": "netease-publish-request-v1", "output_type": "command-response-v1"
}


def show_operation(config: WorkspaceConfig, operation_id: str) -> dict[str, Any]:
    path = _operation_path(config, operation_id)
    if not path.is_file():
        return response(status="missing_input", workspace=str(config.config_path), operation_id=operation_id,
                        validation={"operation": "failed"}, diagnostics=["unknown operation_id"])
    return _public(config, read_json(path))


def resume_operation(config: WorkspaceConfig, operation_id: str) -> dict[str, Any]:
    record = read_json(_operation_path(config, operation_id))
    if record.get("status") == "uncertain":
        record["next_action"] = {"type": "reconcile", "operation_id": operation_id,
                                 "reason": "query the original upload; never resubmit an unknown result"}
    return _public(config, record)


def reconcile_operation(config: WorkspaceConfig, operation_id: str) -> dict[str, Any]:
    path = _operation_path(config, operation_id)
    record = read_json(path)
    if record.get("status") == "completed" and record.get("commit"):
        from .media_operations import _authority_digest
        if record["commit"].get("digest") != _authority_digest(record):
            return response(status="uncertain", workspace=str(config.config_path), operation_id=operation_id,
                            validation={"authoritative_record": "failed"},
                            diagnostics=["authoritative operation digest is invalid"],
                            next_action={"type": "maintenance"})
        _project_receipts(record)
        return _public(config, record, "repaired_receipt_projection")
    attempt = record.get("current_attempt")
    adapter = NETEASE_ADAPTERS.get(attempt.get("adapter_id")) if isinstance(attempt, dict) else None
    if not adapter or getattr(adapter, "adapter_identity", None) != record["intent"].get("adapter_identity"):
        record.update(status="uncertain", diagnostics=["pinned adapter is unavailable; do not resubmit"],
                      next_action={"type": "user", "reason": "restore the pinned adapter"})
        from .media_operations import _commit_authority
        _commit_authority(path, record)
        return _public(config, record)
    try:
        checked = adapter.reconcile(attempt)
    except Exception as exc:
        record.update(status="uncertain", diagnostics=[f"remote receipt query failed: {exc}"],
                      next_action={"type": "reconcile", "operation_id": operation_id})
        from .media_operations import _commit_authority
        _commit_authority(path, record)
        return _public(config, record)
    if checked.get("state") == "completed":
        candidates = _matches(_all_remote(adapter), attempt["filename"], attempt["output_sha256"],
                              attempt.get("duration"))
        if len(candidates) == 1 and _identity(candidates[0], attempt["filename"], attempt["output_sha256"]):
            item = candidates[0]
            checked = {"state": "available", "query_handle": attempt["query_handle"],
                       "remote_id": item["id"], "filename": item["filename"],
                       "sha256": item.get("sha256"), "duration": item.get("duration"), "visible": True}
    record.setdefault("reconciliation", []).append({key: checked.get(key) for key in
                                                      ("state", "query_handle", "remote_id", "filename", "sha256", "visible")})
    candidate = {"id": checked.get("remote_id"), "filename": checked.get("filename"),
                 "sha256": checked.get("sha256"), "duration": checked.get("duration"),
                 "visible": checked.get("visible")}
    if checked.get("state") == "available" and _identity(candidate, record["intent"]["filename"],
                                                           record["intent"]["output_sha256"]):
        return _finish(config, path, record, Path(record["request"]["package"]), candidate,
                       reuse="reconciled_remote_object")
    record.update(status="uncertain", validation={"remote_object_identity": "failed"},
                  diagnostics=["remote receipt/object identity is not verifiable; do not resubmit"],
                  next_action={"type": "reconcile", "operation_id": operation_id})
    from .media_operations import _commit_authority
    _commit_authority(path, record)
    return _public(config, record)
