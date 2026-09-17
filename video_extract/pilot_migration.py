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


def _event(operation: str, status: str, expected: str, actual: str) -> dict[str, str]:
    return {"operation": operation, "status": status, "expected": expected,
            "actual": actual, "recorded_at": _now()}


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
    routes = list(thread.get("thread", {}).get("return_route") or [])
    entries = list(thread.get("thread", {}).get("entry_history") or [])
    locators = [item for item in questions if item.get("locator")]
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
                       "operation_receipts": len(receipts), "return_route": len(routes),
                       "entry_history": len(entries), "locators": len(locators)},
            "missing_facts": missing}


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
             "acceptance": {"automated_fixture": "pending", "real_pilot": "pending_authorization"},
             "events": [_event("migration plan", "passed", "inventory one authorized pilot",
                               "legacy input unchanged; local plan recorded")],
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
    return_route = []
    for frame in thread_raw.get("return_route") or []:
        old_question = str(frame["question_id"])
        if old_question not in mapping: raise ValueError(f"return route references unknown question: {old_question}")
        return_route.append({"module_id": module_id, "thread_id": thread_id,
                             "question_id": mapping[old_question],
                             "entered_at": str(frame.get("entered_at") or created)})
    entry_history = []
    for index, entry in enumerate(thread_raw.get("entry_history") or []):
        old_from, old_to = str(entry["from_question_id"]), str(entry["to_question_id"])
        if old_from not in mapping or old_to not in mapping:
            raise ValueError("entry history references an unknown question")
        entry_history.append({"entry_id": _id("entry", config.workspace_id or "", batch,
                                               str(entry.get("id") or index)),
                              "from_question_id": mapping[old_from], "to_question_id": mapping[old_to],
                              "original_text": str(entry.get("original_text") or "legacy entry"),
                              "relation": entry.get("relation", "deepens"),
                              "created_at": str(entry.get("created_at") or created)})
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
            "revision": 1, "created_at": created, "return_route": return_route,
            "entry_history": entry_history}},
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
                 "legacy_locators": {identity_map[str(item["id"])]: item["locator"]
                                     for item in legacy_thread["questions"] if item.get("locator")},
                 "missing_facts": value["inventory"]["missing_facts"]}
    atomic_write_json(staging / "conversion.json", converted)
    value.update(status="converted", converted_at=_now(), identity_map=identity_map)
    value["events"].append(_event("migration convert", "passed", "target contracts and identity map",
                                  "workspace v2/source v6/learning v2 candidate written"))
    atomic_write_json(root / "batch.json", value)
    return response(status="completed", workspace=str(config.config_path), result={
        "batch": batch, "identity_map": identity_map, "source_version": source_version,
        "missing_facts": converted["missing_facts"]}, validation={"target_contract": "written",
        "legacy_v1_modified": "no"})


def verify(config: WorkspaceConfig, batch: str) -> dict[str, Any]:
    from .learning import _deep_validate, _validate
    from .source_registry import _directory_manifest
    root, value = _state(config, batch)
    conversion_path = root / "converted/conversion.json"
    if not conversion_path.is_file(): raise ValueError("batch has not been converted")
    converted = read_json(conversion_path)
    target_source_digest, _ = _directory_manifest(root / "converted/course")
    source_files = value["package_snapshot"]; target_files = _tree(root / "converted/course")
    locator_checks = []
    for locator in converted["legacy_locators"].values():
        relative = locator.get("path")
        document = (root / "converted/course" / relative).resolve(strict=False) if isinstance(relative, str) else None
        course_root = (root / "converted/course").resolve(strict=False)
        present = bool(document and course_root in document.parents and document.is_file())
        heading = locator.get("heading")
        locator_checks.append(present and (not heading or f"# {heading}" in document.read_text(encoding="utf-8")))
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
              "return_route": sum(len(item.get("return_route", []))
                                  for item in converted["record"]["threads"].values())
                  == value["inventory"]["counts"]["return_route"],
              "entry_history": sum(len(item.get("entry_history", []))
                                   for item in converted["record"]["threads"].values())
                  == value["inventory"]["counts"]["entry_history"],
              "locators": len(converted["legacy_locators"])
                  == value["inventory"]["counts"]["locators"] and all(locator_checks),
              "legacy_source_version": converted["legacy_source_version"]
                  == value["inventory"].get("source_version"),
              "target_source_version": converted["source_version"]
                  == "source-version-" + target_source_digest,
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
        value["acceptance"]["automated_fixture"] = "passed"
        value["events"].append(_event("migration verify", "passed",
                                      "counts, content, images, versions, history, position",
                                      "all deterministic checks passed"))
        atomic_write_json(root / "batch.json", value)
    else:
        value["events"].append(_event("migration verify", "failed",
                                      "counts, content, images, versions, history, position",
                                      "one or more deterministic checks failed"))
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


def _legacy_modes(package: Path, thread_path: Path) -> dict[str, Any]:
    modes: dict[str, int] = {}
    for path in sorted((package, *package.rglob("*"))):
        if path.is_symlink(): continue
        relative = "." if path == package else path.relative_to(package).as_posix()
        modes[relative] = path.stat().st_mode & 0o777
    return {"package": modes, "thread": thread_path.stat().st_mode & 0o777}


def _set_legacy_read_only(package: Path, thread_path: Path, modes: dict[str, Any]) -> None:
    for relative, mode in modes["package"].items():
        path = package if relative == "." else package / relative
        if path.exists() and not path.is_symlink(): path.chmod(int(mode) & ~0o222)
    thread_path.chmod(int(modes["thread"]) & ~0o222)


def _restore_legacy_modes(package: Path, thread_path: Path, modes: dict[str, Any]) -> None:
    thread_path.chmod(int(modes["thread"]))
    for relative, mode in sorted(modes["package"].items(), key=lambda item: item[0].count("/"), reverse=True):
        path = package if relative == "." else package / relative
        if path.exists() and not path.is_symlink(): path.chmod(mode)


def _directory_modes(root: Path) -> dict[str, int]:
    modes: dict[str, int] = {}
    for path in sorted((root, *root.rglob("*"))):
        if path.is_symlink(): continue
        relative = "." if path == root else path.relative_to(root).as_posix()
        modes[relative] = path.stat().st_mode & 0o777
    return modes


def _set_directory_read_only(root: Path, modes: dict[str, int]) -> None:
    for relative, mode in modes.items():
        path = root if relative == "." else root / relative
        if path.exists() and not path.is_symlink(): path.chmod(int(mode) & ~0o222)


def _restore_directory_modes(root: Path, modes: dict[str, int]) -> None:
    for relative, mode in sorted(modes.items(), key=lambda item: item[0].count("/"), reverse=True):
        path = root if relative == "." else root / relative
        if path.exists() and not path.is_symlink(): path.chmod(int(mode))


def cutover(config: WorkspaceConfig, batch: str, authorization_path: Path) -> dict[str, Any]:
    from . import learning
    from .source_registry import register
    results, _ = _require_workspace(config); root, value = _state(config, batch)
    if value["status"] not in {"verified", "cutover"}:
        raise ValueError("batch must pass verify before cutover")
    authorization = read_json(authorization_path.expanduser().resolve())
    expected_authorization = {"batch": batch, "legacy_package": value["legacy_package"],
                              "legacy_thread": value["legacy_thread"], "approved": True}
    if any(authorization.get(key) != expected for key, expected in expected_authorization.items()):
        raise ValueError("cutover authorization must explicitly approve this batch, package, and thread")
    authorization_sha256 = _digest(authorization)
    converted = read_json(root / "converted/conversion.json")
    if not _unchanged(value):
        value["events"].append(_event("migration cutover", "failed",
                                      "verified source remains unchanged",
                                      "legacy source changed after verification"))
        atomic_write_json(root / "batch.json", value)
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        validation={"source_unchanged": "failed", "verified_batch": "passed"},
                        diagnostics=["legacy source changed after verification; rerun plan/convert/verify"])
    if _digest(converted) != value["verified_conversion_sha256"]:
        value["events"].append(_event("migration cutover", "failed",
                                      "verified conversion digest remains unchanged",
                                      "converted generation changed after verification"))
        atomic_write_json(root / "batch.json", value)
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        validation={"converted_generation": "failed"}, diagnostics=["converted generation changed"])
    owners = _ownership(config)
    existing_owner = owners["batches"].get(batch)
    if existing_owner is not None:
        target = Path(existing_owner.get("migrated_course", ""))
        if (existing_owner.get("owner") == "new" and target.is_dir()
                and _tree(target) == _tree(root / "converted/course")):
            value.update(status="cutover", cutover_at=existing_owner["cutover_at"],
                         migrated_course=str(target),
                         learning_commit_id=existing_owner["learning_commit_id"])
            atomic_write_json(root / "batch.json", value)
            return response(status="completed", workspace=str(config.config_path), result={
                "batch": batch, "migrated_course": str(target),
                "source_id": existing_owner["source_id"],
                "source_version": existing_owner["source_version"],
                "learning_commit_id": existing_owner["learning_commit_id"], "reconciled": True},
                validation={"verified_batch": "passed", "source_unchanged": "passed",
                            "single_owner": "new", "legacy_read_only": "passed"})
        raise ValueError("batch already has a different ownership decision")
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
    from .package_lock import package_lock
    with package_lock(learning._roots(config)[2].parent):
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
    package_path, thread_path = Path(value["legacy_package"]), Path(value["legacy_thread"])
    legacy_modes = value.get("legacy_modes_before_cutover")
    if legacy_modes is None:
        legacy_modes = _legacy_modes(package_path, thread_path)
        value["legacy_modes_before_cutover"] = legacy_modes
        value["authorization_sha256"] = authorization_sha256
        value["acceptance"]["real_pilot"] = "authorized_for_cutover"
        atomic_write_json(root / "batch.json", value)
    _set_legacy_read_only(package_path, thread_path, legacy_modes)
    if os.environ.get("VIDEO_EXTRACT_MIGRATION_TEST_FAULT") == "after_legacy_read_only":
        raise OSError("injected failure after legacy read-only transition")
    owners["batches"][batch] = {"owner": "new", "legacy_read_only": True,
        "legacy_package": value["legacy_package"], "legacy_thread": value["legacy_thread"],
        "migrated_course": str(target),
        "source_id": converted["source_id"], "source_version": actual_version,
        "learning_commit_id": published["commit_id"], "cutover_at": _now(), "cutover_baseline": baseline,
        "legacy_modes": legacy_modes, "locators": converted["legacy_locators"],
        "learning_ids": {key: sorted(record[key]) for key in
                         ("modules", "threads", "questions", "relationships", "feedbacks",
                          "preparations", "explanations")},
        "authorization_sha256": authorization_sha256}
    atomic_write_json(_ownership_path(config), owners)
    value.update(status="cutover", cutover_at=_now(), migrated_course=str(target),
                 learning_commit_id=published["commit_id"])
    value["events"].append(_event("migration cutover", "passed",
                                  "verified unchanged batch with one writable owner",
                                  "new store active; legacy package made read-only"))
    atomic_write_json(root / "batch.json", value)
    return response(status="completed", workspace=str(config.config_path), result={
        "batch": batch, "migrated_course": str(target), "source_id": converted["source_id"],
        "source_version": actual_version, "learning_commit_id": published["commit_id"]},
        validation={"verified_batch": "passed", "source_unchanged": "passed",
                    "single_owner": "new", "legacy_read_only": "passed"})


def rollback(config: WorkspaceConfig, batch: str) -> dict[str, Any]:
    results, local = _require_workspace(config); root, value = _state(config, batch)
    owners = _ownership(config); owner = owners["batches"].get(batch)
    if not owner:
        raise ValueError("only a cut-over batch owned by the new store can be rolled back")
    receipt_root = results / "operation-receipts"
    if owner["owner"] == "legacy":
        if not owner.get("receipt_modes_restored", True):
            _restore_directory_modes(receipt_root, owner["receipt_modes_before_rollback"])
            owner["receipt_modes_restored"] = True
            atomic_write_json(_ownership_path(config), owners)
        value.update(status="rolled_back", rollback_at=owner["rollback_at"],
                     preserved_increment=owner["preserved_increment"])
        if not any(event["operation"] == "migration rollback" and event["status"] == "passed"
                   for event in value["events"]):
            value["events"].append(_event("migration rollback", "passed",
                                          "reconcile durable legacy ownership",
                                          "batch state reconstructed after interrupted rollback"))
        atomic_write_json(root / "batch.json", value)
        return response(status="completed", workspace=str(config.config_path), result={
            "batch": batch, "reconciled": True, "receipt_modes_restored": True,
            "preserved_increment": owner["preserved_increment"]},
            validation={"rollback_recovery": "passed"})
    if value["status"] != "cutover" or owner["owner"] not in {"new", "rolling_back"}:
        raise ValueError("only a cut-over batch owned by the new store can be rolled back")
    target = Path(owner["migrated_course"]); baseline = {item["path"]: item for item in owner["cutover_baseline"]}
    receipt_root.mkdir(parents=True, exist_ok=True)
    if owner["owner"] == "new":
        owner["owner"] = "rolling_back"
        owner["new_modes_before_rollback"] = _directory_modes(target)
        owner["receipt_modes_before_rollback"] = _directory_modes(receipt_root)
        owner["receipt_modes_restored"] = False
        atomic_write_json(_ownership_path(config), owners)
    _set_directory_read_only(target, owner["new_modes_before_rollback"])
    _set_directory_read_only(receipt_root, owner["receipt_modes_before_rollback"])
    if os.environ.get("VIDEO_EXTRACT_MIGRATION_TEST_FAULT") == "after_rollback_frozen":
        raise OSError("injected failure after rollback write freeze")
    current = {item["path"]: item for item in _tree(target)}
    changed = sorted(path for path, fact in current.items() if baseline.get(path) != fact)
    deleted = sorted(set(baseline) - set(current))
    cutover_ns = int(datetime.fromisoformat(owner["cutover_at"]).timestamp() * 1_000_000_000)
    receipt_snapshot = _tree(receipt_root)
    receipts = [path for path in receipt_root.rglob("*") if path.is_file() and path.stat().st_mtime_ns >= cutover_ns]
    stamp = str(time.time_ns()); preserved = local / "migration-preserved" / batch / stamp
    for relative in changed:
        destination = preserved / relative; destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target / relative, destination)
    for receipt in receipts:
        destination = preserved / "operation-receipts" / receipt.relative_to(receipt_root)
        destination.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(receipt, destination)
    learning_changed = False
    learning_pointer = results / "learning/current.json"
    from .package_lock import package_lock
    with package_lock(results / "learning"):
        if learning_pointer.is_file():
            current_learning = read_json(learning_pointer).get("commit_id")
            learning_changed = current_learning != owner["learning_commit_id"]
            if learning_changed:
                shutil.copytree(results / "learning", preserved / "learning")
    if _tree(target) != list(current.values()):
        raise RuntimeError("migrated course changed while rollback was preserving it")
    if _tree(receipt_root) != receipt_snapshot:
        raise RuntimeError("operation receipts changed while rollback was preserving them")
    preserved.mkdir(parents=True, exist_ok=True)
    atomic_write_json(preserved / "rollback-manifest.json", {"schema_version": 1, "batch": batch,
        "created_at": _now(), "changed_course_files": changed, "deleted_course_files": deleted,
        "learning_changed": learning_changed,
        "operation_receipts": [path.relative_to(receipt_root).as_posix() for path in receipts],
        "policy": "preserve_only_no_remote_replay"})
    _restore_legacy_modes(Path(owner["legacy_package"]), Path(owner["legacy_thread"]), owner["legacy_modes"])
    owner.update(owner="legacy", legacy_read_only=False, new_read_only=True,
                 replay_required=bool(changed or deleted or receipts or learning_changed), rollback_at=_now(),
                 preserved_increment=str(preserved))
    atomic_write_json(_ownership_path(config), owners)
    if os.environ.get("VIDEO_EXTRACT_MIGRATION_TEST_FAULT") == "after_rollback_owner":
        raise OSError("injected failure after rollback ownership transition")
    _restore_directory_modes(receipt_root, owner["receipt_modes_before_rollback"])
    owner["receipt_modes_restored"] = True
    atomic_write_json(_ownership_path(config), owners)
    if os.environ.get("VIDEO_EXTRACT_MIGRATION_TEST_FAULT") == "after_receipt_modes_restored":
        raise OSError("injected failure after receipt mode restoration")
    value.update(status="rolled_back", rollback_at=_now(), preserved_increment=str(preserved))
    value["events"].append(_event("migration rollback", "passed",
                                  "preserve post-cutover results and receipts without overwrite/replay",
                                  "increments preserved; legacy ownership restored"))
    atomic_write_json(root / "batch.json", value)
    return response(status="completed", workspace=str(config.config_path), result={
        "batch": batch, "preserved_increment": str(preserved), "changed_course_files": changed,
        "deleted_course_files": deleted, "learning_changed": learning_changed,
        "operation_receipts": len(receipts),
        "replay_required": bool(changed or deleted or receipts or learning_changed)},
        validation={"increment_preserved": "passed", "new_store_not_overwritten": "passed",
                    "remote_operations_replayed": "no"})


def locate_migrated_question(config: WorkspaceConfig, question_id: str) -> dict[str, Any] | None:
    owners = _ownership(config)
    for batch, owner in owners["batches"].items():
        locator = owner.get("locators", {}).get(question_id)
        if owner.get("owner") != "new" or not isinstance(locator, dict): continue
        relative = locator.get("path")
        if not isinstance(relative, str): continue
        course = Path(owner["migrated_course"]).resolve(strict=False)
        document = (course / relative).resolve(strict=False)
        if course not in document.parents or not document.is_file(): continue
        return {"batch": batch, "document_path": str(document),
                "heading": locator.get("heading"), "kind": "legacy_migrated_locator"}
    return None


def assert_learning_write_owned(config: WorkspaceConfig, previous: dict[str, Any],
                                proposed: dict[str, Any]) -> None:
    changed: dict[str, set[str]] = {}
    for key in ("modules", "threads", "questions", "relationships", "feedbacks",
                "preparations", "explanations"):
        identities = set(previous[key]) | set(proposed[key])
        changed[key] = {identity for identity in identities
                        if previous[key].get(identity) != proposed[key].get(identity)}
    for batch, owner in _ownership(config)["batches"].items():
        if owner.get("owner") == "new": continue
        protected = owner.get("learning_ids", {})
        protected_questions = set(protected.get("questions", []))
        protected_threads = set(protected.get("threads", []))
        protected_modules = set(protected.get("modules", []))
        linked = any(
            (key == "feedbacks" and item.get("question_id") in protected_questions)
            or (key == "preparations" and item.get("question_id") in protected_questions)
            or (key == "explanations" and item.get("root_question_id") in protected_questions)
            or (key in {"questions", "relationships"} and item.get("thread_id") in protected_threads)
            or (key == "threads" and item.get("module_id") in protected_modules)
            for key, identities in changed.items() for identity in identities
            for item in [proposed[key].get(identity) or previous[key].get(identity) or {}]
        )
        if linked or any(changed[key] & set(protected.get(key, [])) for key in changed):
            raise WorkspaceError(f"migration batch {batch} is owned by the legacy store; new learning location is read-only")
