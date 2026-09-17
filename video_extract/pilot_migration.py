"""Explicit, batch-scoped conversion of one legacy course package and learning thread."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .command_response import response
from .manifest import atomic_write_json, read_json
from .workspace import PORTABLE_SCHEMA_VERSION, WorkspaceConfig, WorkspaceError


BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_workspace(config: WorkspaceConfig) -> tuple[Path, Path]:
    if config.schema_version != PORTABLE_SCHEMA_VERSION or config.results is None or config.local is None:
        raise WorkspaceError("migration requires workspace schema v2")
    return config.results, config.local


def _batch_root(config: WorkspaceConfig, batch: str) -> Path:
    _, local = _require_workspace(config)
    if not BATCH_RE.fullmatch(batch):
        raise ValueError("batch must contain only letters, digits, dot, underscore, or hyphen")
    root = local / "migration-batches" / batch
    if root.is_symlink():
        raise WorkspaceError("migration batch root must not be a symbolic link")
    return root


def _tree(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"legacy package must be a real directory: {root}")
    entries = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.is_symlink():
            raise ValueError(f"legacy package contains a symbolic link: {path}")
        body = path.read_bytes()
        entries.append({"path": path.relative_to(root).as_posix(), "size": len(body),
                        "sha256": hashlib.sha256(body).hexdigest()})
    return entries


def _file_fact(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"legacy thread must be a real file: {path}")
    body = path.read_bytes()
    return {"path": str(path.resolve()), "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _id(kind: str, workspace_id: str, batch: str, legacy_id: str) -> str:
    seed = f"{workspace_id}:migration:{batch}:{kind}:{legacy_id}".encode()
    # Stable mapping with RFC 4122 version/variant bits matching the target UUIDv4 contract.
    value = uuid.UUID(bytes=hashlib.sha256(seed).digest()[:16], version=4)
    return f"{kind}-{value}"


def _load_legacy(package: Path, thread_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_path = package / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("legacy package is missing manifest.json")
    manifest, thread = read_json(manifest_path), read_json(thread_path)
    if not isinstance(manifest, dict) or not isinstance(thread, dict):
        raise ValueError("legacy manifest and thread must be JSON objects")
    if not isinstance(thread.get("questions"), list) or not thread["questions"]:
        raise ValueError("legacy thread must contain at least one question")
    return manifest, thread


def _inventory(package: Path, manifest: dict[str, Any], thread: dict[str, Any]) -> dict[str, Any]:
    files = _tree(package)
    notes = [item for item in files if Path(item["path"]).suffix.lower() in {".md", ".txt"}]
    images = [item for item in files if Path(item["path"]).suffix.lower() in IMAGE_SUFFIXES]
    questions = thread["questions"]
    relations = [item for item in questions if item.get("parent_id")]
    image_references: list[dict[str, Any]] = []
    for note in notes:
        note_path = package / note["path"]
        for target in re.findall(r"!\[[^]]*\]\(([^)]+)\)", note_path.read_text(encoding="utf-8")):
            external = "://" in target
            resolved = (note_path.parent / target).resolve(strict=False) if not external else None
            inside = bool(resolved and (resolved == package or package in resolved.parents))
            image_references.append({"note": note["path"], "target": target,
                                     "external": external, "present": bool(inside and resolved.is_file())})
    feedbacks = list(thread.get("feedbacks") or [])
    receipts = list(thread.get("operation_receipts") or [])
    missing = []
    if not feedbacks: missing.append("feedbacks")
    if not receipts: missing.append("operation_receipts")
    if not manifest.get("source_version"): missing.append("source_version")
    return {"title": manifest.get("title"), "legacy_identity": manifest.get("identity"),
            "legacy_schema_version": manifest.get("schema_version"),
            "source_version": manifest.get("source_version"), "files": files,
            "notes": [item["path"] for item in notes], "images": [item["path"] for item in images],
            "image_references": image_references,
            "question_ids": [str(item.get("id")) for item in questions],
            "current_question_id": thread.get("thread", {}).get("current_question_id"),
            "counts": {"notes": len(notes), "images": len(images), "questions": len(questions),
                       "relationships": len(relations), "feedbacks": len(feedbacks),
                       "operation_receipts": len(receipts)}, "missing_facts": missing}


def plan(config: WorkspaceConfig, batch: str, package: Path, thread_path: Path) -> dict[str, Any]:
    from .source_registry import _git_basis
    root = _batch_root(config, batch)
    if root.exists():
        raise ValueError(f"migration batch already exists: {batch}")
    package, thread_path = package.expanduser().resolve(), thread_path.expanduser().resolve()
    manifest, thread = _load_legacy(package, thread_path)
    inventory = _inventory(package, manifest, thread)
    value = {"schema_version": 1, "batch": batch, "status": "planned", "created_at": _now(),
             "workspace_id": config.workspace_id, "legacy_package": str(package),
             "legacy_thread": str(thread_path), "package_snapshot": inventory["files"],
             "thread_snapshot": _file_fact(thread_path), "inventory": inventory,
             "engineering_basis": _git_basis(config.project),
             "target_contracts": {"workspace": 2, "source_package": 6, "learning_record": 2}}
    root.mkdir(parents=True)
    atomic_write_json(root / "batch.json", value)
    return response(status="completed", workspace=str(config.config_path), result=value,
                    validation={"legacy_package": "passed", "legacy_thread": "passed",
                                "scope": "one_package_one_thread"})


def _state(config: WorkspaceConfig, batch: str) -> tuple[Path, dict[str, Any]]:
    root = _batch_root(config, batch); path = root / "batch.json"
    if not path.is_file(): raise ValueError(f"unknown migration batch: {batch}")
    return root, read_json(path)


def _unchanged(value: dict[str, Any]) -> bool:
    try:
        return (_tree(Path(value["legacy_package"])) == value["package_snapshot"]
                and _file_fact(Path(value["legacy_thread"])) == value["thread_snapshot"])
    except (OSError, ValueError):
        return False


def _converted_record(config: WorkspaceConfig, batch: str, legacy: dict[str, Any],
                      source_id: str, source_version: str) -> tuple[dict[str, Any], dict[str, str]]:
    created = _now(); module_raw = legacy.get("module") or {}; thread_raw = legacy.get("thread") or {}
    questions_raw = legacy["questions"]
    mapping = {str(item["id"]): _id("question", config.workspace_id or "", batch, str(item["id"]))
               for item in questions_raw}
    module_id = _id("module", config.workspace_id or "", batch, "module")
    thread_id = _id("thread", config.workspace_id or "", batch, str(thread_raw.get("id") or "thread"))
    root_old = str(thread_raw.get("root_question_id") or questions_raw[0]["id"])
    current_old = str(thread_raw.get("current_question_id") or root_old)
    if root_old not in mapping or current_old not in mapping:
        raise ValueError("legacy root/current question is not present in questions")
    source_ref = {"source_id": source_id, "source_version": source_version, "role": "course_fact"}
    questions = {mapping[str(item["id"])]: {
        "question_id": mapping[str(item["id"])], "thread_id": thread_id,
        "original_question": str(item.get("text") or item.get("title") or item["id"]),
        "title": str(item.get("title") or item.get("text") or item["id"]),
        "created_at": str(item.get("created_at") or created),
        "unresolved_confusions": list(item.get("unresolved_confusions") or []), "explanation_refs": [],
    } for item in questions_raw}
    relationships = {}
    for item in questions_raw:
        if not item.get("parent_id"): continue
        child_old, parent_old = str(item["id"]), str(item["parent_id"])
        if parent_old not in mapping: raise ValueError(f"unknown legacy parent question: {parent_old}")
        relation_id = _id("relationship", config.workspace_id or "", batch, f"{parent_old}:{child_old}")
        relationships[relation_id] = {"relationship_id": relation_id, "thread_id": thread_id,
            "from_question_id": mapping[parent_old], "to_question_id": mapping[child_old],
            "type": item.get("relation", "deepens"), "created_at": str(item.get("created_at") or created)}
    feedbacks = {}
    for index, item in enumerate(legacy.get("feedbacks") or []):
        old_question = str(item.get("question_id"))
        if old_question not in mapping: raise ValueError(f"feedback references unknown question: {old_question}")
        feedback_id = _id("feedback", config.workspace_id or "", batch, str(item.get("id") or index))
        feedbacks[feedback_id] = {"feedback_id": feedback_id, "question_id": mapping[old_question],
            "state": item["state"], "original_text": item["text"],
            "created_at": str(item.get("created_at") or created)}
    record = {"schema_version": 2,
        "modules": {module_id: {"module_id": module_id, "goal": str(module_raw.get("goal") or "Legacy course"),
            "scope": str(module_raw.get("scope") or "Migrated legacy scope"), "source_refs": [source_ref],
            "thread_ids": [thread_id], "last_active_thread_id": thread_id, "created_at": created}},
        "threads": {thread_id: {"thread_id": thread_id, "module_id": module_id,
            "root_question_id": mapping[root_old], "current_question_id": mapping[current_old],
            "revision": 1, "created_at": created, "return_route": [], "entry_history": []}},
        "questions": questions, "relationships": relationships, "feedbacks": feedbacks,
        "preparations": {}, "explanations": {}}
    return record, {**mapping, str(thread_raw.get("id") or "thread"): thread_id,
                    "module": module_id, "source": source_id}


def convert(config: WorkspaceConfig, batch: str) -> dict[str, Any]:
    from .source_registry import _directory_manifest
    root, value = _state(config, batch)
    if value["status"] not in {"planned", "converted", "verified"}:
        raise ValueError(f"batch cannot be converted from status {value['status']}")
    if not _unchanged(value):
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        validation={"source_unchanged": "failed"}, diagnostics=["legacy source changed since plan"])
    staging = root / "converted"; course = staging / "course"
    if staging.exists(): shutil.rmtree(staging)
    course.parent.mkdir(parents=True); shutil.copytree(value["legacy_package"], course)
    content_digest, _ = _directory_manifest(course)
    source_id = _id("source", config.workspace_id or "", batch, "course")
    source_version = "source-version-" + content_digest
    legacy_thread = read_json(Path(value["legacy_thread"]))
    record, identity_map = _converted_record(config, batch, legacy_thread, source_id, source_version)
    converted = {"schema_version": 1, "batch": batch, "created_at": _now(),
                 "source_id": source_id, "source_version": source_version,
                 "record": record, "identity_map": identity_map,
                 "legacy_source_version": value["inventory"].get("source_version"),
                 "legacy_operation_receipts": list(legacy_thread.get("operation_receipts") or []),
                 "missing_facts": value["inventory"]["missing_facts"]}
    atomic_write_json(staging / "conversion.json", converted)
    value.update(status="converted", converted_at=_now(), identity_map=identity_map)
    atomic_write_json(root / "batch.json", value)
    return response(status="completed", workspace=str(config.config_path), result={
        "batch": batch, "identity_map": identity_map, "source_version": source_version,
        "missing_facts": converted["missing_facts"]}, validation={"target_contract": "written",
        "legacy_v1_modified": "no"})


def verify(config: WorkspaceConfig, batch: str) -> dict[str, Any]:
    from .learning import _deep_validate, _validate
    root, value = _state(config, batch)
    conversion_path = root / "converted/conversion.json"
    if not conversion_path.is_file(): raise ValueError("batch has not been converted")
    converted = read_json(conversion_path)
    source_files = value["package_snapshot"]; target_files = _tree(root / "converted/course")
    checks = {"content": source_files == target_files,
              "notes": len(value["inventory"]["notes"]) == value["inventory"]["counts"]["notes"],
              "images": len(value["inventory"]["images"]) == value["inventory"]["counts"]["images"],
              "image_references": all(item["external"] or item["present"]
                                      for item in value["inventory"]["image_references"]),
              "questions": len(converted["record"]["questions"]) == value["inventory"]["counts"]["questions"],
              "relationships": len(converted["record"]["relationships"]) == value["inventory"]["counts"]["relationships"],
              "feedbacks": len(converted["record"]["feedbacks"]) == value["inventory"]["counts"]["feedbacks"],
              "operation_receipts": len(converted["legacy_operation_receipts"])
                  == value["inventory"]["counts"]["operation_receipts"],
              "current_question": converted["record"]["threads"][converted["identity_map"][read_json(Path(value["legacy_thread"]))["thread"]["id"]]]["current_question_id"]
                  == converted["identity_map"][value["inventory"]["current_question_id"]],
              "source_unchanged": _unchanged(value)}
    snapshot = {"schema_version": 2, "commit_id": "learning-commit-" + "0" * 64,
                "parent_commit_id": None, "revision": 1, "created_at": _now(),
                "record": converted["record"], "objects": {}}
    try:
        _validate("learning-snapshot-v2.schema.json", snapshot); _deep_validate(snapshot)
        checks["learning_record_v2"] = True
    except WorkspaceError:
        checks["learning_record_v2"] = False
    status = "completed" if all(checks.values()) else "failed"
    if status == "completed":
        value.update(status="verified", verified_at=_now(), verified_conversion_sha256=_digest(converted),
                     verified_source_sha256=_digest({"package": value["package_snapshot"],
                                                    "thread": value["thread_snapshot"]}))
        atomic_write_json(root / "batch.json", value)
    return response(status=status, workspace=str(config.config_path), result={
        "batch": batch, "identity_map": converted["identity_map"], "counts": value["inventory"]["counts"],
        "missing_facts": converted["missing_facts"]},
        validation={key: "passed" if passed else "failed" for key, passed in checks.items()},
        diagnostics=[] if status == "completed" else ["migration verification failed"])


def _ownership_path(config: WorkspaceConfig) -> Path:
    results, _ = _require_workspace(config); return results / "migration-ownership.json"


def _ownership(config: WorkspaceConfig) -> dict[str, Any]:
    path = _ownership_path(config)
    return read_json(path) if path.is_file() else {"schema_version": 1, "batches": {}}


def cutover(config: WorkspaceConfig, batch: str) -> dict[str, Any]:
    from . import learning
    from .source_registry import register
    results, _ = _require_workspace(config); root, value = _state(config, batch)
    if value["status"] != "verified": raise ValueError("batch must pass verify before cutover")
    converted = read_json(root / "converted/conversion.json")
    if not _unchanged(value):
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        validation={"source_unchanged": "failed", "verified_batch": "passed"},
                        diagnostics=["legacy source changed after verification; rerun plan/convert/verify"])
    if _digest(converted) != value["verified_conversion_sha256"]:
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        validation={"converted_generation": "failed"}, diagnostics=["converted generation changed"])
    owners = _ownership(config)
    if batch in owners["batches"]: raise ValueError("batch already has an ownership decision")
    target = results / "migrated-courses" / batch
    if target.exists():
        if _tree(target) != _tree(root / "converted/course"):
            raise ValueError(f"migration target exists with different content: {target}")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(root / "converted/course", target)
    registered = register(config, target, title=read_json(target / "manifest.json").get("title"),
                          explicit_source_id=converted["source_id"])
    if registered["status"] != "completed": raise WorkspaceError("converted source registration failed")
    actual_version = registered["result"]["source_version"]
    record = converted["record"]
    for module in record["modules"].values(): module["source_refs"][0]["source_version"] = actual_version
    previous = learning._load(config); merged = json.loads(json.dumps(previous["record"]))
    changed = False
    for key in ("modules", "threads", "questions", "relationships", "feedbacks", "preparations", "explanations"):
        for identity, item in record[key].items():
            existing = merged[key].get(identity)
            if existing is not None and existing != item:
                raise WorkspaceError(f"migration identity collision in {key}: {identity}")
            if existing is None:
                merged[key][identity] = item; changed = True
    published = learning._publish(config, previous, merged) if changed else previous
    baseline = _tree(target)
    owners["batches"][batch] = {"owner": "new", "legacy_read_only": True,
        "legacy_package": value["legacy_package"], "migrated_course": str(target),
        "source_id": converted["source_id"], "source_version": actual_version,
        "learning_commit_id": published["commit_id"], "cutover_at": _now(), "cutover_baseline": baseline}
    atomic_write_json(_ownership_path(config), owners)
    value.update(status="cutover", cutover_at=_now(), migrated_course=str(target),
                 learning_commit_id=published["commit_id"])
    atomic_write_json(root / "batch.json", value)
    return response(status="completed", workspace=str(config.config_path), result={
        "batch": batch, "migrated_course": str(target), "source_id": converted["source_id"],
        "source_version": actual_version, "learning_commit_id": published["commit_id"]},
        validation={"verified_batch": "passed", "source_unchanged": "passed",
                    "single_owner": "new", "legacy_read_only": "passed"})


def rollback(config: WorkspaceConfig, batch: str) -> dict[str, Any]:
    results, local = _require_workspace(config); root, value = _state(config, batch)
    owners = _ownership(config); owner = owners["batches"].get(batch)
    if value["status"] != "cutover" or not owner or owner["owner"] != "new":
        raise ValueError("only a cut-over batch owned by the new store can be rolled back")
    target = Path(owner["migrated_course"]); baseline = {item["path"]: item for item in owner["cutover_baseline"]}
    current = {item["path"]: item for item in _tree(target)}
    changed = sorted(path for path, fact in current.items() if baseline.get(path) != fact)
    receipt_root = results / "operation-receipts"
    cutover_ns = int(datetime.fromisoformat(owner["cutover_at"]).timestamp() * 1_000_000_000)
    receipts = [path for path in receipt_root.rglob("*") if path.is_file() and path.stat().st_mtime_ns >= cutover_ns] if receipt_root.is_dir() else []
    stamp = str(time.time_ns()); preserved = local / "migration-preserved" / batch / stamp
    for relative in changed:
        destination = preserved / relative; destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target / relative, destination)
    for receipt in receipts:
        destination = preserved / "operation-receipts" / receipt.relative_to(receipt_root)
        destination.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(receipt, destination)
    preserved.mkdir(parents=True, exist_ok=True)
    atomic_write_json(preserved / "rollback-manifest.json", {"schema_version": 1, "batch": batch,
        "created_at": _now(), "changed_course_files": changed,
        "operation_receipts": [path.relative_to(receipt_root).as_posix() for path in receipts],
        "policy": "preserve_only_no_remote_replay"})
    owner.update(owner="legacy", legacy_read_only=False, new_read_only=True,
                 replay_required=bool(changed or receipts), rollback_at=_now(),
                 preserved_increment=str(preserved))
    atomic_write_json(_ownership_path(config), owners)
    value.update(status="rolled_back", rollback_at=_now(), preserved_increment=str(preserved))
    atomic_write_json(root / "batch.json", value)
    return response(status="completed", workspace=str(config.config_path), result={
        "batch": batch, "preserved_increment": str(preserved), "changed_course_files": changed,
        "operation_receipts": len(receipts), "replay_required": bool(changed or receipts)},
        validation={"increment_preserved": "passed", "new_store_not_overwritten": "passed",
                    "remote_operations_replayed": "no"})
