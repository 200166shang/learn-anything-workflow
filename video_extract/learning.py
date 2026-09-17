"""Authoritative learning-record-v2 store and first-root workflow."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from .command_response import response
from .manifest import atomic_write_json, read_json
from .package_lock import package_lock
from .source_registry import verify as verify_source
from .workspace import PORTABLE_SCHEMA_VERSION, WorkspaceConfig, WorkspaceError

SCHEMA_ROOT = Path(__file__).resolve().parent.parent / "schemas"
_SCHEMAS = {name: json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8")) for name in (
    "learning-record-v2.schema.json", "learning-snapshot-v2.schema.json", "learning-candidate-v2.schema.json"
)}
_REGISTRY = Registry().with_resources((value["$id"], Resource.from_contents(value)) for value in _SCHEMAS.values())


class LearningPublishError(OSError):
    def __init__(self, message: str, *, stage: str, commit_id: str):
        super().__init__(message); self.stage = stage; self.commit_id = commit_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate(name: str, value: Any) -> None:
    errors = list(Draft202012Validator(_SCHEMAS[name], registry=_REGISTRY,
                                       format_checker=FormatChecker()).iter_errors(value))
    if errors:
        error = errors[0]
        raise WorkspaceError(f"{name} validation failed: {error.message}")


def _roots(config: WorkspaceConfig) -> tuple[Path, Path, Path, Path]:
    if config.schema_version != PORTABLE_SCHEMA_VERSION or config.results is None:
        raise WorkspaceError("learning-record v2 requires workspace schema v2")
    declared_results = config.results.resolve(strict=False)
    root = config.results / "learning"
    if root.is_symlink() or root.resolve(strict=False) != declared_results / "learning":
        raise WorkspaceError(f"refusing symbolic link inside declared results root: {root}")
    for child in (root / "objects", root / "commits", root / "current.json", root / "candidates"):
        if child.is_symlink():
            raise WorkspaceError(f"refusing symbolic link inside declared results root: {child}")
    return root / "objects", root / "commits", root / "current.json", root / "candidates"


def _object_path(config: WorkspaceConfig, digest: str) -> Path:
    object_root = _roots(config)[0]
    prefix = object_root / digest[:2]
    if prefix.is_symlink():
        raise WorkspaceError(f"refusing symbolic link inside declared results root: {prefix}")
    path = prefix / digest
    if path.is_symlink():
        raise WorkspaceError(f"refusing symbolic link inside declared results root: {path}")
    return path


def _empty() -> dict[str, Any]:
    return {"schema_version": 2, "commit_id": None, "parent_commit_id": None, "revision": 0,
            "created_at": None, "record": {"schema_version": 2, "modules": {}, "threads": {},
                                              "questions": {}, "relationships": {}, "feedbacks": {},
                                              "preparations": {}, "explanations": {}}, "objects": {}}


def _logical_object_path(digest: str) -> str:
    return f"learning/objects/{digest[:2]}/{digest}"


def _deep_validate(value: dict[str, Any]) -> None:
    record = value["record"]; modules = record["modules"]; threads = record["threads"]
    questions = record["questions"]; explanations = record["explanations"]
    for module_id, module in modules.items():
        if module["module_id"] != module_id:
            raise WorkspaceError(f"module identity mismatch: {module_id}")
        owned = {thread_id for thread_id, thread in threads.items() if thread["module_id"] == module_id}
        if set(module["thread_ids"]) != owned:
            raise WorkspaceError(f"module thread ownership is incomplete or duplicated: {module_id}")
        if module["last_active_thread_id"] is not None and module["last_active_thread_id"] not in owned:
            raise WorkspaceError(f"module last_active_thread_id is not owned: {module_id}")
    for thread_id, thread in threads.items():
        if thread["thread_id"] != thread_id or thread["module_id"] not in modules:
            raise WorkspaceError(f"thread identity or module reference is invalid: {thread_id}")
        for field in ("root_question_id", "current_question_id"):
            question = questions.get(thread[field])
            if question is None or question["thread_id"] != thread_id:
                raise WorkspaceError(f"thread {field} is not owned by thread: {thread_id}")
        reachable = {thread["root_question_id"]}; pending = [thread["root_question_id"]]
        while pending:
            current = pending.pop()
            for relation in record["relationships"].values():
                if relation["thread_id"] == thread_id and relation["from_question_id"] == current \
                        and relation["to_question_id"] not in reachable:
                    reachable.add(relation["to_question_id"]); pending.append(relation["to_question_id"])
        owned_questions = {question_id for question_id, question in questions.items()
                           if question["thread_id"] == thread_id}
        if reachable != owned_questions:
            raise WorkspaceError(f"thread contains questions unreachable from its root: {thread_id}")
    for question_id, question in questions.items():
        if question["question_id"] != question_id or question["thread_id"] not in threads:
            raise WorkspaceError(f"question identity or thread reference is invalid: {question_id}")
    for relationship_id, relation in record["relationships"].items():
        endpoints = (questions.get(relation["from_question_id"]), questions.get(relation["to_question_id"]))
        if relation["relationship_id"] != relationship_id or relation["thread_id"] not in threads \
                or any(item is None or item["thread_id"] != relation["thread_id"] for item in endpoints):
            raise WorkspaceError(f"relationship crosses or misses thread ownership: {relationship_id}")
    for feedback_id, feedback in record["feedbacks"].items():
        if feedback["feedback_id"] != feedback_id or feedback["question_id"] not in questions:
            raise WorkspaceError(f"feedback reference is invalid: {feedback_id}")
    for preparation_id, preparation in record["preparations"].items():
        question = questions.get(preparation["question_id"])
        calculated_scope = hashlib.sha256(json.dumps(preparation["source_refs"], sort_keys=True).encode()).hexdigest()
        if preparation["preparation_id"] != preparation_id or question is None \
                or preparation["prepared_revision"] > value["revision"] \
                or preparation["source_scope_sha256"] != calculated_scope:
            raise WorkspaceError(f"preparation reference is invalid: {preparation_id}")
    for explanation_id, explanation in explanations.items():
        root = questions.get(explanation["root_question_id"])
        if explanation["explanation_id"] != explanation_id or root is None \
                or threads[root["thread_id"]]["root_question_id"] != root["question_id"]:
            raise WorkspaceError(f"explanation root ownership is invalid: {explanation_id}")
        if str(explanation["current_revision"]) not in explanation["revisions"]:
            raise WorkspaceError(f"explanation current revision is missing: {explanation_id}")
        for revision_key, revision in explanation["revisions"].items():
            if revision["revision"] != int(revision_key) or revision["object_sha256"] not in value["objects"] \
                    or revision["logical_path"] != _logical_object_path(revision["object_sha256"]):
                raise WorkspaceError(f"explanation revision is not completely reachable: {explanation_id}/{revision_key}")
            for question_id, section_ids in revision["section_map"].items():
                if question_id not in questions or questions[question_id]["thread_id"] != root["thread_id"] or not section_ids:
                    raise WorkspaceError(f"explanation section map crosses thread ownership: {question_id}")
            if "affected_question_ids" in revision and set(revision["affected_question_ids"]) != set(revision["section_map"]):
                raise WorkspaceError(f"explanation affected questions do not match its section map: {explanation_id}/{revision_key}")
        correction_ids: set[str] = set()
        for correction in explanation.get("confirmed_corrections", []):
            correction_id = correction["correction_id"]
            introduced = explanation["revisions"].get(str(correction["introduced_revision"]))
            if correction_id in correction_ids or introduced is None \
                    or correction_id not in introduced.get("introduced_correction_ids", []):
                raise WorkspaceError(f"confirmed correction history is invalid: {explanation_id}/{correction_id}")
            correction_ids.add(correction_id)
    for question_id, question in questions.items():
        for ref in question["explanation_refs"]:
            explanation = explanations.get(ref["explanation_id"])
            revision = explanation and explanation["revisions"].get(str(ref["explanation_revision"]))
            if revision is None or ref["section_id"] not in revision["section_map"].get(question_id, []) \
                    or ref["object_sha256"] != revision["object_sha256"] \
                    or ref["logical_path"] != revision["logical_path"]:
                raise WorkspaceError(f"question explanation locator is invalid: {question_id}")


def _validate_candidate_store(config: WorkspaceConfig) -> list[Path]:
    candidates = _roots(config)[3]
    if not candidates.exists():
        return []
    validated: list[Path] = []
    for path in sorted(candidates.glob("candidate-*.json")):
        if path.is_symlink():
            raise WorkspaceError(f"learning candidate must not be a symbolic link: {path}")
        candidate = read_json(path); _validate("learning-candidate-v2.schema.json", candidate)
        if candidate.get("proposal_format") == "immutable-v1":
            proposal_digest = hashlib.sha256(json.dumps(candidate["proposal"], ensure_ascii=False,
                                                        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if candidate["proposal_sha256"] != proposal_digest:
                raise WorkspaceError(f"learning candidate proposal is corrupt: {path}")
            object_ref = candidate["proposal"]["draft_object"]
            object_path = _object_path(config, object_ref["sha256"])
            if (not object_path.is_file() or object_path.stat().st_size != object_ref["size"]
                    or hashlib.sha256(object_path.read_bytes()).hexdigest() != object_ref["sha256"]
                    or object_ref["logical_path"] != _logical_object_path(object_ref["sha256"])):
                raise WorkspaceError(f"learning candidate draft object is missing or corrupt: {path}")
        validated.append(path)
    return validated


def _load(config: WorkspaceConfig) -> dict[str, Any]:
    _, commits, pointer, _ = _roots(config)
    if not pointer.is_file():
        return _empty()
    selected = read_json(pointer)
    manifest = commits / f'{selected["commit_id"]}.json'
    if not manifest.is_file() or hashlib.sha256(manifest.read_bytes()).hexdigest() != selected.get("manifest_sha256"):
        raise WorkspaceError("learning snapshot manifest is missing or corrupt")
    value = read_json(manifest)
    _validate("learning-snapshot-v2.schema.json", value)
    for digest, item in value["objects"].items():
        path = _object_path(config, digest)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest or path.stat().st_size != item["size"]:
            raise WorkspaceError(f"learning snapshot object is missing or corrupt: {digest}")
    _deep_validate(value)
    _validate_candidate_store(config)
    return value


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish(config: WorkspaceConfig, previous: dict[str, Any], record: dict[str, Any],
             objects: dict[str, Any] | None = None) -> dict[str, Any]:
    object_root, commits, pointer, _ = _roots(config)
    value = {"schema_version": 2, "parent_commit_id": previous.get("commit_id"),
             "revision": previous["revision"] + 1, "created_at": _now(), "record": record,
             "objects": {**previous.get("objects", {}), **(objects or {})}}
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    value["commit_id"] = "learning-commit-" + hashlib.sha256(encoded).hexdigest()
    _validate("learning-snapshot-v2.schema.json", value)
    _deep_validate(value)
    body = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"
    commits.mkdir(parents=True, exist_ok=True)
    manifest = commits / f'{value["commit_id"]}.json'
    if not manifest.exists():
        with manifest.open("xb") as stream:
            stream.write(body); stream.flush(); os.fsync(stream.fileno())
    _sync_directory(commits)
    if os.environ.get("VIDEO_EXTRACT_LEARNING_TEST_FAULT") == "before_publish":
        raise LearningPublishError("injected failure before learning publish", stage="before_publish",
                                   commit_id=value["commit_id"])
    atomic_write_json(pointer, {"schema_version": 2, "commit_id": value["commit_id"],
                                "manifest_sha256": hashlib.sha256(body).hexdigest()})
    _sync_directory(pointer.parent)
    if os.environ.get("VIDEO_EXTRACT_LEARNING_TEST_FAULT") == "after_publish":
        raise LearningPublishError("injected failure after learning publish", stage="after_publish",
                                   commit_id=value["commit_id"])
    return value


def _revision_conflict(config: WorkspaceConfig, snapshot: dict[str, Any], expected: int,
                       operation: str, proposal: dict[str, Any]) -> dict[str, Any] | None:
    if expected == snapshot["revision"]:
        return None
    candidates = _roots(config)[3]
    created = not candidates.exists()
    candidates.mkdir(parents=True, exist_ok=True)
    if created:
        if os.environ.get("VIDEO_EXTRACT_LEARNING_TEST_FAULT") == "candidate_parent_sync":
            raise OSError("injected candidate parent directory sync failure")
        _sync_directory(candidates.parent)
    path = candidates / f"candidate-{uuid.uuid4()}.json"
    candidate = {"schema_version": 2, "operation": operation,
                 "expected_revision": expected, "observed_revision": snapshot["revision"],
                 "proposal": proposal}
    if operation == "explanation.commit" and "draft_object" in proposal:
        candidate["proposal_format"] = "immutable-v1"
        candidate["proposal_sha256"] = hashlib.sha256(json.dumps(
            proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    _validate("learning-candidate-v2.schema.json", candidate)
    atomic_write_json(path, candidate)
    try:
        if os.environ.get("VIDEO_EXTRACT_LEARNING_TEST_FAULT") in {"candidate_sync", "candidate_cleanup_sync"}:
            raise OSError("injected candidate directory sync failure")
        _sync_directory(candidates)
    except OSError:
        path.unlink(missing_ok=True)
        if os.environ.get("VIDEO_EXTRACT_LEARNING_TEST_FAULT") == "candidate_cleanup_sync":
            raise OSError("injected candidate cleanup directory sync failure")
        _sync_directory(candidates)
        raise
    return response(status="awaiting_user", workspace=str(config.config_path), result={
        "candidate": str(path), "observed_revision": snapshot["revision"]},
        validation={"expected_revision": "conflict"},
        next_action={"type": "user", "reason": "resolve learning revision conflict"})


def publish_failure_response(config: WorkspaceConfig, error: LearningPublishError) -> dict[str, Any]:
    visible = error.stage == "after_publish"
    return response(status="recoverable_failure", workspace=str(config.config_path), result={
        "commit_id": error.commit_id, "logical_visibility": "current" if visible else "not_current",
        "durability": "unknown"}, validation={"learning_snapshot": "uncertain",
        "durability": "unknown"}, diagnostics=[str(error)], next_action={
            "type": "reconcile" if visible else "retry",
            "command": (f"video-extract learning reconcile --commit-id {error.commit_id} "
                        f"--workspace {config.config_path} --json") if visible else None})


def reconcile(config: WorkspaceConfig, commit_id: str) -> dict[str, Any]:
    snapshot = _load(config)
    if snapshot["commit_id"] != commit_id:
        return response(status="recoverable_failure", workspace=str(config.config_path), result={
            "commit_id": commit_id, "logical_visibility": "not_current"},
            diagnostics=["requested learning commit is not current"])
    objects, commits, pointer, _ = _roots(config)
    for digest in snapshot["objects"]:
        _sync_directory((objects / digest[:2] / digest).parent)
    _sync_directory(commits); _sync_directory(pointer.parent)
    return response(status="completed", workspace=str(config.config_path), result={
        "commit_id": commit_id, "revision": snapshot["revision"], "durability": "confirmed"},
        validation={"learning_snapshot": "passed", "objects": "passed", "durability": "passed"})


def backup_entries(config: WorkspaceConfig) -> dict[str, Any]:
    """Return the complete pinned learning generation for a future unified backup."""
    snapshot = _load(config); objects, commits, pointer, _ = _roots(config)
    candidate_paths = _validate_candidate_store(config); candidate_objects: set[str] = set()
    for path in candidate_paths:
        candidate = read_json(path)
        if candidate.get("proposal_format") == "immutable-v1":
            candidate_objects.add(candidate["proposal"]["draft_object"]["sha256"])
    object_digests = set(snapshot["objects"]) | candidate_objects
    return {"store": "learning-snapshot-v2", "commit_id": snapshot["commit_id"],
            "entries": [str(pointer), str(commits / f'{snapshot["commit_id"]}.json'),
                        *[str(objects / digest[:2] / digest) for digest in sorted(object_digests)],
                        *map(str, candidate_paths)]}


def _source_context(config: WorkspaceConfig, source_refs: list[dict[str, str]], *,
                    trusted_historical: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    context: list[dict[str, Any]] = []; errors: list[str] = []
    for ref in source_refs:
        try:
            checked = verify_source(config, ref["source_id"], ref["source_version"])
        except (OSError, ValueError, WorkspaceError) as exc:
            errors.append(f'{ref["source_id"]}@{ref["source_version"]}: {exc}'); continue
        if checked["status"] != "completed":
            errors.extend(checked.get("diagnostics") or [f'cannot verify {ref["source_id"]}']); continue
        item = {**ref, "kind": checked["result"]["kind"], "title": checked["result"]["title"],
                "content": checked["result"].get("content"), "location": checked["result"].get("location"),
                "availability": checked["result"].get("availability"),
                "version_state": checked["result"].get("version_state", "unknown"),
                "current_version": checked["result"].get("current_version"),
                "entries": checked["result"].get("entries", []),
                "provenance": checked["result"].get("provenance"),
                "provenance_status": checked["result"].get("provenance_status", "unknown"),
                "version_basis": checked["result"]["version_basis"]}
        context.append(item)
        if not trusted_historical and item["availability"] not in {"available_from_results", "available_at_location"}:
            errors.append(f'{ref["source_id"]}@{ref["source_version"]}: {item["availability"]}')
        elif not trusted_historical and item["version_state"] != "current":
            errors.append(f'{ref["source_id"]}@{ref["source_version"]}: historical; current is {item["current_version"]}')
    return context, errors


def _sources_blocked(config: WorkspaceConfig, question: dict[str, Any] | None,
                     context: list[dict[str, Any]], errors: list[str]) -> dict[str, Any]:
    return response(status="missing_input", workspace=str(config.config_path), result={
        "question": question, "source_context": context, "blocked_source_errors": errors},
        validation={"learning_record": "passed", "sources": "failed"},
        diagnostics=["required source scope is missing, corrupt, or version-mismatched", *errors],
        next_action={"type": "user", "reason": "restore or relocate only the source versions required by this module"})


def create_module(config: WorkspaceConfig, goal: str, scope: str, source_id: str,
                  source_version: str, expected_revision: int | None = None,
                  source_role: str | None = None) -> dict[str, Any]:
    if not goal.strip() or not scope.strip():
        return response(status="missing_input", workspace=str(config.config_path),
                        diagnostics=["confirmed module goal and scope must be non-empty"])
    context, source_errors = _source_context(config, [{"source_id": source_id, "source_version": source_version}])
    if source_errors:
        return _sources_blocked(config, None, context, source_errors)
    role = source_role or ("current_code" if context[0]["kind"] == "code" else "course_fact")
    if role not in {"course_fact", "current_code", "supplemental_source"}:
        return response(status="missing_input", workspace=str(config.config_path),
                        validation={"source_role": "failed"}, diagnostics=[f"invalid source role: {role}"])
    if (role == "current_code") != (context[0]["kind"] == "code") and role != "supplemental_source":
        return response(status="missing_input", workspace=str(config.config_path),
                        validation={"source_role": "failed"},
                        diagnostics=[f"source role {role} does not match source kind {context[0]['kind']}"])
    module_id = "module-" + str(uuid.uuid4())
    with package_lock(_roots(config)[2].parent):
        snapshot = _load(config)
        if expected_revision is not None:
            conflict = _revision_conflict(config, snapshot, expected_revision, "module.create", {"goal": goal, "scope": scope})
            if conflict: return conflict
        module = {"module_id": module_id, "goal": goal.strip(), "scope": scope.strip(),
                  "source_refs": [{"source_id": source_id, "source_version": source_version, "role": role}],
                  "thread_ids": [], "last_active_thread_id": None, "created_at": _now()}
        record = {**snapshot["record"], "modules": {**snapshot["record"]["modules"], module_id: module}}
        published = _publish(config, snapshot, record)
    return response(status="completed", workspace=str(config.config_path),
                    operation_id="operation-" + str(uuid.uuid4()), result={"module": module,
                    "learning_schema_version": 2, "revision": published["revision"],
                    "commit_id": published["commit_id"]}, validation={"learning_record": "passed",
                    "source_version": "passed"}, provenance={"source_version": source_version})


def show_module(config: WorkspaceConfig, module_id: str) -> dict[str, Any]:
    snapshot = _load(config)
    module = snapshot["record"]["modules"].get(module_id)
    if module is None:
        return response(status="missing_input", workspace=str(config.config_path),
                        diagnostics=[f"unknown module_id: {module_id}"])
    source_checks = []
    for ref in module["source_refs"]:
        checked = verify_source(config, ref["source_id"], ref["source_version"])
        value = checked.get("result") or {}
        availability = value.get("availability", "missing")
        status = value.get("change_check", "unknown")
        if availability not in {"available_from_results", "available_at_location"}:
            status = availability
        source_checks.append({**ref, "status": status,
                              "current_version": value.get("current_version"),
                              "availability": availability})
    return response(status="completed", workspace=str(config.config_path), result={"module": module,
                    "source_checks": source_checks,
                    "learning_schema_version": 2, "revision": snapshot["revision"],
                    "commit_id": snapshot["commit_id"]}, validation={"learning_record": "passed"})


def recommend_roots(config: WorkspaceConfig, module_id: str) -> dict[str, Any]:
    snapshot = _load(config)
    module = snapshot["record"]["modules"].get(module_id)
    if module is None:
        return response(status="missing_input", workspace=str(config.config_path),
                        diagnostics=[f"unknown module_id: {module_id}"])
    context, errors = _source_context(config, module["source_refs"])
    if errors:
        return _sources_blocked(config, None, context, errors)
    return response(status="awaiting_model", workspace=str(config.config_path), result={
        "module": module, "source_context": context, "persisted": False,
    }, validation={"learning_record": "passed", "sources": "passed"}, next_action={
        "type": "model", "action": "recommend_root_questions",
        "instructions": "Recommend a small set of root questions, explain how each connects the confirmed goal and scope, and wait for the user to choose. Do not persist unselected candidates.",
    })


def _question(question_id: str, thread_id: str, text: str) -> dict[str, Any]:
    return {"question_id": question_id, "thread_id": thread_id, "original_question": text.strip(),
            "title": text.strip(), "created_at": _now(), "unresolved_confusions": [],
            "explanation_refs": []}


def create_thread(config: WorkspaceConfig, module_id: str, root_text: str,
                  expected_revision: int | None = None) -> dict[str, Any]:
    if not root_text.strip():
        return response(status="missing_input", workspace=str(config.config_path),
                        diagnostics=["root question must be non-empty"])
    with package_lock(_roots(config)[2].parent):
        snapshot = _load(config); record = snapshot["record"]
        if expected_revision is not None:
            conflict = _revision_conflict(config, snapshot, expected_revision, "thread.create", {"module_id": module_id, "root_question": root_text})
            if conflict: return conflict
        module = record["modules"].get(module_id)
        if module is None:
            return response(status="missing_input", workspace=str(config.config_path),
                            diagnostics=[f"unknown module_id: {module_id}"])
        thread_id = "thread-" + str(uuid.uuid4()); question_id = "question-" + str(uuid.uuid4())
        question = _question(question_id, thread_id, root_text)
        thread = {"thread_id": thread_id, "module_id": module_id, "root_question_id": question_id,
                  "current_question_id": question_id, "revision": 1, "created_at": _now()}
        updated_module = {**module, "thread_ids": [*module["thread_ids"], thread_id],
                          "last_active_thread_id": thread_id}
        updated = {**record, "modules": {**record["modules"], module_id: updated_module},
                   "threads": {**record["threads"], thread_id: thread},
                   "questions": {**record["questions"], question_id: question}}
        published = _publish(config, snapshot, updated)
    return response(status="completed", workspace=str(config.config_path), result={"thread": thread,
                    "question": question, "revision": published["revision"],
                    "commit_id": published["commit_id"]}, validation={"learning_record": "passed"})


def pursue(config: WorkspaceConfig, thread_id: str, from_question_id: str, relation: str,
           text: str, expected_revision: int | None = None) -> dict[str, Any]:
    if not text.strip():
        return response(status="missing_input", workspace=str(config.config_path),
                        diagnostics=["actual question must be non-empty"])
    if relation not in {"deepens", "applies", "related"}:
        return response(status="missing_input", workspace=str(config.config_path),
                        diagnostics=["relation must be deepens, applies, or related"])
    with package_lock(_roots(config)[2].parent):
        snapshot = _load(config); record = snapshot["record"]
        if expected_revision is not None:
            conflict = _revision_conflict(config, snapshot, expected_revision, "learning.pursue", {"thread_id": thread_id, "question": text})
            if conflict: return conflict
        thread = record["threads"].get(thread_id); source = record["questions"].get(from_question_id)
        if thread is None or source is None or source["thread_id"] != thread_id:
            return response(status="missing_input", workspace=str(config.config_path),
                            diagnostics=["unknown thread/from-question relationship"])
        question_id = "question-" + str(uuid.uuid4()); relationship_id = "relationship-" + str(uuid.uuid4())
        question = _question(question_id, thread_id, text)
        relationship = {"relationship_id": relationship_id, "thread_id": thread_id,
                        "from_question_id": from_question_id, "to_question_id": question_id,
                        "type": relation, "created_at": _now()}
        updated_thread = {**thread, "current_question_id": question_id, "revision": thread["revision"] + 1}
        module_id = thread["module_id"]; module = record["modules"][module_id]
        updated = {**record,
                   "modules": {**record["modules"], module_id: {**module, "last_active_thread_id": thread_id}},
                   "threads": {**record["threads"], thread_id: updated_thread},
                   "questions": {**record["questions"], question_id: question},
                   "relationships": {**record["relationships"], relationship_id: relationship}}
        published = _publish(config, snapshot, updated)
    return response(status="completed", workspace=str(config.config_path), result={"question": question,
                    "relationship": relationship, "thread": updated_thread,
                    "revision": published["revision"], "commit_id": published["commit_id"]},
                    validation={"learning_record": "passed"})


def show_thread(config: WorkspaceConfig, thread_id: str) -> dict[str, Any]:
    snapshot = _load(config); record = snapshot["record"]; thread = record["threads"].get(thread_id)
    if thread is None:
        return response(status="missing_input", workspace=str(config.config_path), diagnostics=[f"unknown thread_id: {thread_id}"])
    return response(status="completed", workspace=str(config.config_path), result={"thread": thread,
                    "questions": [value for value in record["questions"].values() if value["thread_id"] == thread_id],
                    "relationships": [value for value in record["relationships"].values() if value["thread_id"] == thread_id],
                    "revision": snapshot["revision"], "commit_id": snapshot["commit_id"]},
                    validation={"learning_record": "passed"})


def _parse_code_locator(locator: dict[str, Any]) -> tuple[str, str, str]:
    value = str(locator.get("value", ""))
    kind = locator.get("kind")
    if kind == "symbol" and "::" in value:
        path, symbol = value.split("::", 1)
        if path and symbol:
            return path, "symbol", symbol
    if kind == "line_range" and ":" in value:
        path, lines = value.rsplit(":", 1)
        if path and re.fullmatch(r"[1-9][0-9]*-[1-9][0-9]*", lines):
            start, end = map(int, lines.split("-", 1))
            if start <= end:
                return path, "line_range", lines
    raise ValueError("current_code locator must be file::symbol or file:start-end with matching locator kind")


def _evidence_source_checks(config: WorkspaceConfig, evidence_refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for evidence in evidence_refs:
        checked = verify_source(config, evidence["source_id"], evidence["source_version"])
        value = checked.get("result") or {}
        availability = value.get("availability", "missing")
        check_status = value.get("change_check", "unknown") if checked.get("status") == "completed" else "missing"
        if availability not in {"available_from_results", "available_at_location"}:
            check_status = availability
        if evidence["claim_type"] == "current_code" and value.get("version_state") == "historical":
            try:
                path, _, _ = _parse_code_locator(evidence.get("locator") or {})
            except ValueError:
                check_status = "needs_review"
            else:
                current = verify_source(config, evidence["source_id"], value["current_version"])
                current_value = current.get("result") or {}
                current_entries = {entry["path"]: entry["sha256"] for entry in current_value.get("entries", [])}
                expected_digest = (evidence.get("locator") or {}).get("content_sha256")
                if (current.get("status") == "completed"
                        and current_value.get("availability") == "available_at_location"
                        and expected_digest and current_entries.get(path) == expected_digest):
                    check_status = "current"
                else:
                    check_status = "needs_review" if current.get("status") == "completed" else "missing"
        checks.append({"source_id": evidence["source_id"], "source_version": evidence["source_version"],
                       "claim_type": evidence["claim_type"], "locator": evidence["locator"],
                       "status": check_status, "availability": availability,
                       "current_version": value.get("current_version")})
    return checks


def locate(config: WorkspaceConfig, question_id: str) -> dict[str, Any]:
    snapshot = _load(config); question = snapshot["record"]["questions"].get(question_id)
    if question is None:
        return response(status="missing_input", workspace=str(config.config_path), diagnostics=[f"unknown question_id: {question_id}"])
    if not question["explanation_refs"]:
        return response(status="awaiting_model", workspace=str(config.config_path), result={
            "question": question, "explanation_state": "pending"},
            next_action={"type": "model", "action": "prepare_explanation"})
    locations = [{**ref, "document_path": str(config.results / ref["logical_path"])}
                 for ref in question["explanation_refs"]]
    evidence_refs: list[dict[str, Any]] = []
    for ref in question["explanation_refs"]:
        explanation = snapshot["record"]["explanations"][ref["explanation_id"]]
        revision = explanation["revisions"][str(ref["explanation_revision"])]
        evidence_refs.extend(revision["evidence_refs"])
    source_checks = _evidence_source_checks(config, evidence_refs)
    explanation_state = ("needs_review" if any(item["status"] != "current" for item in source_checks)
                         else "available")
    return response(status="completed", workspace=str(config.config_path), result={
        "question": question, "explanation_state": explanation_state, "locations": locations,
        "source_checks": source_checks})


PROFILES = {
    "linear_transform": "Explain intuition, causal linear-combination reasoning, basis images and coordinates, a numeric vector example, and the boundary between linear, affine, and homogeneous forms.",
    "recognition_to_action": "Trace actual detection data through coordinates, decision, and execution; cover stale/failed input and separate observed code from recommendations.",
    "frame_pipeline": "Trace the actual entry, producer, queue, consumer, output, and concurrency boundary; cover slow, full, and shutdown behavior without guessing architecture.",
}


def prepare_explanation(config: WorkspaceConfig, question_id: str, profile: str) -> dict[str, Any]:
    if profile not in PROFILES:
        return response(status="missing_input", workspace=str(config.config_path),
                        diagnostics=[f"unknown teaching profile: {profile}"])
    snapshot = _load(config); record = snapshot["record"]; question = record["questions"].get(question_id)
    if question is None:
        return response(status="missing_input", workspace=str(config.config_path), diagnostics=[f"unknown question_id: {question_id}"])
    thread = record["threads"][question["thread_id"]]; module = record["modules"][thread["module_id"]]
    trusted_evidence_refs: list[dict[str, Any]] | None = None
    if question["explanation_refs"]:
        evidence_refs: list[dict[str, Any]] = []
        for ref in question["explanation_refs"]:
            explanation = record["explanations"][ref["explanation_id"]]
            evidence_refs.extend(explanation["revisions"][str(ref["explanation_revision"])]["evidence_refs"])
        source_checks = _evidence_source_checks(config, evidence_refs)
        if any(item["status"] != "current" for item in source_checks):
            return response(status="awaiting_user", workspace=str(config.config_path), result={
                "question": question, "explanation_state": "needs_review", "source_checks": source_checks,
                "locations": [{**ref, "document_path": str(config.results / ref["logical_path"])}
                              for ref in question["explanation_refs"]]},
                validation={"learning_record": "passed", "sources": "needs_review"},
                diagnostics=["the existing explanation has evidence that is missing, version-mismatched, or changed"],
                next_action={"type": "user", "reason": "review only the affected evidence before revising the explanation"})
        trusted_evidence_refs = evidence_refs
    if trusted_evidence_refs is None:
        context_refs = module["source_refs"]
        context, errors = _source_context(config, context_refs)
    else:
        roles = {(ref["source_id"], ref["source_version"]): ref["role"] for ref in module["source_refs"]}
        seen: set[tuple[str, str]] = set()
        context_refs = []
        for evidence in trusted_evidence_refs:
            identity = (evidence["source_id"], evidence["source_version"])
            if identity not in seen:
                seen.add(identity)
                context_refs.append({"source_id": identity[0], "source_version": identity[1],
                                     "role": roles[identity]})
        context, errors = _source_context(config, context_refs, trusted_historical=True)
    if errors:
        return _sources_blocked(config, question, context, errors)
    section_id = "section-" + str(uuid.uuid4()); preparation_id = "preparation-" + str(uuid.uuid4())
    source_scope = hashlib.sha256(json.dumps(module["source_refs"], sort_keys=True).encode()).hexdigest()
    with package_lock(_roots(config)[2].parent):
        snapshot = _load(config); record = snapshot["record"]
        current_question = record["questions"].get(question_id)
        if current_question is None:
            return response(status="missing_input", workspace=str(config.config_path), diagnostics=[f"unknown question_id: {question_id}"])
        preparation = {"preparation_id": preparation_id, "question_id": question_id, "profile": profile,
                       "section_id": section_id, "prepared_revision": snapshot["revision"] + 1,
                       "source_refs": module["source_refs"], "source_scope_sha256": source_scope,
                       "created_at": _now(), "consumed_at": None}
        updated = {**record, "preparations": {**record["preparations"], preparation_id: preparation}}
        published = _publish(config, snapshot, updated)
    return response(status="awaiting_model", workspace=str(config.config_path), result={
        "question": question, "profile": profile, "section_id": section_id, "preparation_id": preparation_id,
        "prepared_revision": published["revision"],
        "required_marker": f"<!-- section-id: {section_id} -->", "source_context": context,
        "claim_types": ["course_fact", "current_code", "supplemental_source", "inference"],
    }, validation={"learning_record": "passed", "sources": "passed"}, next_action={
        "type": "model", "action": "write_complete_explanation", "instructions": PROFILES[profile],
    })


def _quality_errors(text: str, profile: str, review: dict[str, Any]) -> list[str]:
    required = {"intuition", "causality", "mechanism", "worked_example", "conditions", "source_alignment"}
    required |= {
        "linear_transform": {"basis_coordinate_reasoning", "affine_boundary"},
        "recognition_to_action": {"actual_code_separated", "stale_input_handling"},
        "frame_pipeline": {"actual_entry_verified", "concurrency_verified", "backpressure_shutdown"},
    }[profile]
    errors = [f"teaching review did not confirm {key}" for key in sorted(required) if review.get(key) is not True]
    if profile == "linear_transform":
        if not re.search(r"\d", text): errors.append("linear_transform requires a numeric worked example")
        if not re.search(r"基|basis", text, re.I): errors.append("linear_transform must connect columns to basis vectors")
        if not re.search(r"平移|仿射|齐次|affine|homogeneous", text, re.I): errors.append("linear_transform must state linear/affine boundaries")
    elif profile == "recognition_to_action":
        for label, pattern in (("detection", r"识别|检测|detection"), ("coordinates", r"坐标|coordinate"),
                               ("decision", r"决策|decision"), ("execution", r"执行|action|execute"),
                               ("stale input", r"过期|陈旧|stale|expired")):
            if not re.search(pattern, text, re.I): errors.append(f"recognition_to_action is missing {label}")
    else:
        for label, pattern in (("entry", r"入口|entry"), ("producer", r"生产者|producer"),
                               ("queue", r"队列|queue"), ("consumer", r"消费者|consumer"),
                               ("output", r"出口|结果|output"), ("boundary", r"线程|异步|thread|async"),
                               ("failure behavior", r"慢|满|退出|slow|full|shutdown")):
            if not re.search(pattern, text, re.I): errors.append(f"frame_pipeline is missing {label}")
    return errors


def _store_learning_object(config: WorkspaceConfig, body: bytes) -> tuple[str, Path]:
    digest = hashlib.sha256(body).hexdigest(); path = _object_path(config, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("xb") as stream:
            stream.write(body); stream.flush(); os.fsync(stream.fileno())
    _sync_directory(path.parent); _sync_directory(_roots(config)[0])
    return digest, path


def _confirmed_correction_errors(text: str, corrections: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for correction in corrections:
        if correction["original_claim"] in text:
            errors.append(f"confirmed correction would reintroduce original claim: {correction['correction_id']}")
        if correction["corrected_claim"] not in text:
            errors.append(f"confirmed corrected claim is missing: {correction['correction_id']}")
        applicability = correction.get("applicability")
        if applicability and applicability not in text:
            errors.append(f"confirmed correction applicability is missing: {correction['correction_id']}")
    return errors


def commit_explanation(config: WorkspaceConfig, question_id: str, draft: Path, evidence_path: Path,
                       review_path: Path, profile: str, preparation_id: str,
                       expected_revision: int | None = None, section_map_path: Path | None = None,
                       revision_metadata_path: Path | None = None,
                       corrections_path: Path | None = None,
                       replay_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if replay_payload is None:
        text = draft.read_text(encoding="utf-8")
        evidence = json.loads(evidence_path.read_text(encoding="utf-8")); review = json.loads(review_path.read_text(encoding="utf-8"))
        requested_section_map = json.loads(section_map_path.read_text(encoding="utf-8")) if section_map_path else None
        metadata = json.loads(revision_metadata_path.read_text(encoding="utf-8")) if revision_metadata_path else None
        requested_corrections = json.loads(corrections_path.read_text(encoding="utf-8")) if corrections_path else []
    else:
        text = replay_payload["text"]; evidence = replay_payload["evidence_refs"]
        review = replay_payload["teaching_review"]; requested_section_map = replay_payload["section_map"]
        metadata = {"summary": replay_payload["change_summary"],
                    "affected_question_ids": replay_payload["affected_question_ids"]}
        requested_corrections = replay_payload["corrections"]
    if profile not in PROFILES or not isinstance(evidence, list) or not evidence:
        return response(status="failed", workspace=str(config.config_path), diagnostics=["profile and at least one evidence reference are required"])
    markers = re.findall(r"<!--\s*section-id:\s*(section-[0-9a-f-]{36})\s*-->", text)
    errors = [] if markers and len(markers) == len(set(markers)) else ["draft must contain unique stable section-id markers"]
    errors.extend(_quality_errors(text, profile, review if isinstance(review, dict) else {}))
    if errors:
        return response(status="failed", workspace=str(config.config_path), validation={"teaching_quality": "failed"}, diagnostics=errors)
    body = text.encode(); digest = hashlib.sha256(body).hexdigest()
    with package_lock(_roots(config)[2].parent):
        snapshot = _load(config); record = snapshot["record"]; question = record["questions"].get(question_id)
        if question is None:
            return response(status="missing_input", workspace=str(config.config_path), diagnostics=[f"unknown question_id: {question_id}"])
        thread = record["threads"][question["thread_id"]]; module = record["modules"][thread["module_id"]]
        root_id = thread["root_question_id"]
        existing = next((item for item in record["explanations"].values() if item["root_question_id"] == root_id), None)
        candidate_section_map = requested_section_map or {question_id: markers}
        candidate_revision_kind = ("correction" if requested_corrections else "refactor"
                                   if requested_section_map is not None else "initial" if not existing else "revision")
        candidate_summary = (metadata or {}).get("summary", "Initial explanation" if not existing else "Revised explanation")
        if expected_revision is not None and expected_revision != snapshot["revision"]:
            object_digest, object_path = _store_learning_object(config, body)
            proposal = {"question_id": question_id,
                        "draft_object": {"sha256": object_digest,
                                         "logical_path": _logical_object_path(object_digest), "size": len(body)},
                        "evidence_refs": evidence, "teaching_review": review,
                        "section_map": candidate_section_map,
                        "revision_kind": candidate_revision_kind, "change_summary": candidate_summary,
                        "affected_question_ids": sorted(candidate_section_map),
                        "corrections": requested_corrections, "profile": profile,
                        "preparation_id": preparation_id}
            conflict = _revision_conflict(config, snapshot, expected_revision, "explanation.commit", proposal)
            if conflict:
                conflict["artifact_refs"] = [str(object_path)]
                return conflict
        preparation = record["preparations"].get(preparation_id)
        scope_digest = hashlib.sha256(json.dumps(module["source_refs"], sort_keys=True).encode()).hexdigest()
        if preparation is None or preparation["question_id"] != question_id or preparation["profile"] != profile \
                or preparation["consumed_at"] is not None or preparation["source_refs"] != module["source_refs"] \
                or preparation["prepared_revision"] != snapshot["revision"] \
                or preparation["source_scope_sha256"] != scope_digest or preparation["section_id"] not in markers:
            return response(status="failed", workspace=str(config.config_path),
                            validation={"preparation": "failed"},
                            diagnostics=["preparation token is missing, stale, consumed, cross-question, or marker-mismatched"])
        allowed_refs = {(item["source_id"], item["source_version"]): item["role"] for item in module["source_refs"]}
        evidence_errors: list[str] = []
        for item in evidence:
            if (not isinstance(item, dict)
                    or item.get("claim_type") not in {"course_fact", "current_code", "supplemental_source", "inference"}
                    or not item.get("source_id") or not item.get("source_version")
                    or not item.get("locator") or not item.get("claim")):
                evidence_errors.append("each evidence reference needs source identity, locator, claim, and claim_type"); continue
            identity = (item["source_id"], item["source_version"])
            if identity not in allowed_refs:
                evidence_errors.append("evidence source is outside the question module's confirmed source scope"); continue
            context, source_errors = _source_context(config, [{"source_id": identity[0], "source_version": identity[1]}])
            if source_errors:
                evidence_errors.extend(source_errors); continue
            expected_type = allowed_refs[identity]
            if item["claim_type"] not in {expected_type, "inference"}:
                evidence_errors.append(f"claim_type {item['claim_type']} does not match confirmed source role {expected_type}")
            if item["claim_type"] == "current_code":
                locator = item.get("locator") or {}
                content_digest = locator.get("content_sha256")
                try:
                    path, _, _ = _parse_code_locator(locator)
                except ValueError as exc:
                    evidence_errors.append(str(exc)); continue
                source_context = next((candidate for candidate in context
                                       if (candidate["source_id"], candidate["source_version"]) == identity), None)
                entry_digests = {entry["path"]: entry["sha256"]
                                 for entry in (source_context or {}).get("entries", [])}
                if not content_digest:
                    evidence_errors.append("current_code locator requires content_sha256 with a file::symbol or line-range locator")
                elif path not in entry_digests or entry_digests[path] != content_digest:
                    evidence_errors.append("current_code locator content_sha256 does not match the pinned source file")
        if evidence_errors:
            return response(status="failed", workspace=str(config.config_path),
                            validation={"source_scope": "failed"}, diagnostics=evidence_errors)
        explanation_id = existing["explanation_id"] if existing else "explanation-" + str(uuid.uuid4())
        revision = existing["current_revision"] + 1 if existing else 1
        section_map = requested_section_map or {question_id: markers}
        if not isinstance(section_map, dict) or not section_map:
            return response(status="failed", workspace=str(config.config_path),
                            validation={"same_root_scope": "failed"}, diagnostics=["section map must be a non-empty object"])
        same_root_errors: list[str] = []
        mapped_markers: set[str] = set()
        for mapped_question_id, section_ids in section_map.items():
            mapped_question = record["questions"].get(mapped_question_id)
            if mapped_question is None or mapped_question["thread_id"] != question["thread_id"]:
                same_root_errors.append(f"section map question is outside the selected root: {mapped_question_id}")
            if not isinstance(section_ids, list) or not section_ids:
                same_root_errors.append(f"section map needs at least one section for {mapped_question_id}")
            else:
                mapped_markers.update(section_ids)
        previous_map = (existing or {}).get("revisions", {}).get(str((existing or {}).get("current_revision")), {}).get("section_map", {})
        missing_history = set(previous_map) - set(section_map)
        if missing_history:
            same_root_errors.append("refactor would remove historical question locations: " + ", ".join(sorted(missing_history)))
        if mapped_markers != set(markers) or preparation["section_id"] not in mapped_markers:
            same_root_errors.append("section map must cover every draft marker including the prepared marker")
        if same_root_errors:
            return response(status="failed", workspace=str(config.config_path),
                            validation={"same_root_scope": "failed"}, diagnostics=same_root_errors)
        if metadata is not None and (not isinstance(metadata, dict)
                                     or not str(metadata.get("summary", "")).strip()
                                     or set(metadata.get("affected_question_ids", [])) != set(section_map)):
            return response(status="failed", workspace=str(config.config_path),
                            validation={"revision_metadata": "failed"},
                            diagnostics=["revision metadata must summarize the change and name exactly the affected questions"])
        if not isinstance(requested_corrections, list):
            return response(status="failed", workspace=str(config.config_path),
                            validation={"corrections": "failed"}, diagnostics=["corrections must be a list"])
        confirmed_corrections = list((existing or {}).get("confirmed_corrections", []))
        correction_regressions = _confirmed_correction_errors(text, confirmed_corrections)
        if correction_regressions:
            return response(status="awaiting_user", workspace=str(config.config_path),
                            validation={"confirmed_corrections": "conflict"},
                            diagnostics=correction_regressions,
                            next_action={"type": "user", "reason": "revise the candidate without undoing confirmed corrections"})
        introduced_correction_ids: list[str] = []
        for correction in requested_corrections:
            if (not isinstance(correction, dict) or not str(correction.get("original_claim", "")).strip()
                    or not str(correction.get("corrected_claim", "")).strip()
                    or not str(correction.get("applicability", "")).strip()
                    or not correction.get("evidence_refs") or not correction.get("affected_conclusions")):
                return response(status="failed", workspace=str(config.config_path),
                                validation={"corrections": "failed"},
                                diagnostics=["each correction needs original/corrected claims, applicability, evidence, and affected conclusions"])
            if any((item.get("source_id"), item.get("source_version")) not in allowed_refs
                   for item in correction["evidence_refs"] if isinstance(item, dict)):
                return response(status="failed", workspace=str(config.config_path),
                                validation={"corrections": "failed"},
                                diagnostics=["correction evidence is outside the selected root's confirmed source scope"])
            if (correction["original_claim"] in text or correction["corrected_claim"] not in text
                    or correction["applicability"] not in text):
                return response(status="failed", workspace=str(config.config_path),
                                validation={"corrections": "failed"},
                                diagnostics=["current draft must remove the original claim and contain the corrected claim and applicability"])
            correction_id = "correction-" + str(uuid.uuid4())
            confirmed_corrections.append({"correction_id": correction_id,
                                          "original_claim": correction["original_claim"],
                                          "corrected_claim": correction["corrected_claim"],
                                          "applicability": correction["applicability"],
                                          "evidence_refs": correction["evidence_refs"],
                                          "affected_conclusions": correction["affected_conclusions"],
                                          "confirmed_at": _now(), "introduced_revision": revision})
            introduced_correction_ids.append(correction_id)
        digest, object_path = _store_learning_object(config, body)
        logical_path = _logical_object_path(digest)
        revision_kind = (replay_payload["revision_kind"] if replay_payload is not None else
                         "correction" if introduced_correction_ids else "refactor"
                         if requested_section_map is not None else "initial" if not existing else "revision")
        revision_value = {"revision": revision, "object_sha256": digest, "logical_path": logical_path, "created_at": _now(),
                          "profile": profile, "evidence_refs": evidence, "teaching_review": review,
                          "section_map": section_map, "change_reason": "initial" if not existing else "revised",
                          "revision_kind": revision_kind,
                          "change_summary": (metadata or {}).get("summary", "Initial explanation" if not existing else "Revised explanation"),
                          "affected_question_ids": sorted(section_map),
                          "introduced_correction_ids": introduced_correction_ids}
        explanation = {"explanation_id": explanation_id, "root_question_id": root_id,
                       "current_revision": revision,
                       "revisions": {**(existing or {}).get("revisions", {}), str(revision): revision_value},
                       "confirmed_corrections": confirmed_corrections}
        updated_questions = dict(record["questions"])
        for mapped_question_id, section_ids in section_map.items():
            updated_questions[mapped_question_id] = {**updated_questions[mapped_question_id], "explanation_refs": [
                {"explanation_id": explanation_id, "explanation_revision": revision,
                 "section_id": section_id, "object_sha256": digest, "logical_path": logical_path}
                for section_id in section_ids]}
        updated = {**record, "questions": updated_questions,
                   "preparations": {**record["preparations"], preparation_id: {**preparation, "consumed_at": _now()}},
                   "explanations": {**record["explanations"], explanation_id: explanation}}
        published = _publish(config, snapshot, updated, {digest: {"kind": "explanation_markdown", "size": len(body)}})
    public = {"explanation_id": explanation_id, "root_question_id": root_id, "revision": revision,
              **revision_value, "document_path": str(object_path)}
    return response(status="completed", workspace=str(config.config_path), result={"explanation": public,
                    "commit_id": published["commit_id"], "revision": published["revision"]},
                    artifact_refs=[str(object_path)], validation={"learning_record": "passed",
                    "teaching_quality": "passed", "source_versions": "passed"})


def replay_explanation_candidate(config: WorkspaceConfig, candidate_path: Path) -> dict[str, Any]:
    candidate_root = _roots(config)[3].resolve(strict=False)
    resolved = candidate_path.resolve(strict=True)
    if resolved.parent != candidate_root or resolved.is_symlink():
        return response(status="failed", workspace=str(config.config_path),
                        validation={"candidate": "failed"}, diagnostics=["candidate is outside the workspace candidate store"])
    candidate = read_json(resolved)
    _validate("learning-candidate-v2.schema.json", candidate)
    if candidate["operation"] != "explanation.commit" or candidate.get("proposal_format") != "immutable-v1":
        return response(status="unsupported", workspace=str(config.config_path),
                        validation={"candidate": "unsupported"},
                        diagnostics=["only complete explanation.commit candidates can be replayed"])
    snapshot = _load(config)
    if snapshot["revision"] != candidate["observed_revision"]:
        return response(status="awaiting_user", workspace=str(config.config_path),
                        validation={"candidate_cas": "conflict"}, result={
                            "candidate": str(resolved), "observed_revision": snapshot["revision"],
                            "candidate_observed_revision": candidate["observed_revision"]},
                        next_action={"type": "user", "reason": "review candidate against newer learning state"})
    proposal = candidate["proposal"]; object_ref = proposal["draft_object"]
    object_path = _object_path(config, object_ref["sha256"])
    if (not object_path.is_file() or object_path.stat().st_size != object_ref["size"]
            or hashlib.sha256(object_path.read_bytes()).hexdigest() != object_ref["sha256"]
            or object_ref["logical_path"] != _logical_object_path(object_ref["sha256"])):
        return response(status="failed", workspace=str(config.config_path),
                        validation={"candidate": "failed", "draft_object": "failed"},
                        diagnostics=["candidate draft object is missing, corrupt, or unreachable"])
    payload = {**proposal, "text": object_path.read_text(encoding="utf-8")}
    result = commit_explanation(config, proposal["question_id"], object_path, object_path, object_path,
                                proposal["profile"], proposal["preparation_id"],
                                candidate["observed_revision"], replay_payload=payload)
    if result.get("status") == "completed":
        result["result"]["candidate_replayed"] = str(resolved)
    return result


def restore_explanation(config: WorkspaceConfig, question_id: str, source_revision: int,
                        expected_revision: int | None = None) -> dict[str, Any]:
    with package_lock(_roots(config)[2].parent):
        snapshot = _load(config); record = snapshot["record"]; question = record["questions"].get(question_id)
        if expected_revision is not None:
            conflict = _revision_conflict(config, snapshot, expected_revision, "explanation.restore",
                                          {"question_id": question_id, "source_revision": source_revision})
            if conflict: return conflict
        if question is None:
            return response(status="missing_input", workspace=str(config.config_path),
                            diagnostics=[f"unknown question_id: {question_id}"])
        thread = record["threads"][question["thread_id"]]; root_id = thread["root_question_id"]
        explanation = next((item for item in record["explanations"].values()
                            if item["root_question_id"] == root_id), None)
        source = explanation and explanation["revisions"].get(str(source_revision))
        if source is None:
            return response(status="missing_input", workspace=str(config.config_path),
                            diagnostics=[f"unknown explanation revision for selected root: {source_revision}"])
        current = explanation["revisions"][str(explanation["current_revision"])]
        missing_current_questions = set(current["section_map"]) - set(source["section_map"])
        if missing_current_questions:
            return response(status="failed", workspace=str(config.config_path),
                            validation={"locations": "failed"}, diagnostics=[
                                "restored expression predates current historical question locations: "
                                + ", ".join(sorted(missing_current_questions))])
        text = _object_path(config, source["object_sha256"]).read_text(encoding="utf-8")
        corrections = list(explanation.get("confirmed_corrections", []))
        for correction in corrections:
            if correction["original_claim"] in text:
                text = text.replace(correction["original_claim"], correction["corrected_claim"])
            elif correction["corrected_claim"] not in text:
                return response(status="failed", workspace=str(config.config_path),
                                validation={"corrections": "failed"},
                                diagnostics=[f"cannot safely overlay confirmed correction {correction['correction_id']}"])
            applicability = correction.get("applicability")
            if applicability and applicability not in text:
                text += (f"\n\n<!-- correction-id: {correction['correction_id']} -->\n"
                         f"纠错适用边界：{applicability}\n")
        correction_errors = _confirmed_correction_errors(text, corrections)
        if correction_errors:
            return response(status="failed", workspace=str(config.config_path),
                            validation={"corrections": "failed"}, diagnostics=correction_errors)
        markers = set(re.findall(r"<!--\s*section-id:\s*(section-[0-9a-f-]{36})\s*-->", text))
        required_markers = {section_id for values in source["section_map"].values() for section_id in values}
        if markers != required_markers:
            return response(status="failed", workspace=str(config.config_path),
                            validation={"locations": "failed"},
                            diagnostics=["restored expression no longer contains its complete section map"])
        body = text.encode(); digest = hashlib.sha256(body).hexdigest(); object_path = _object_path(config, digest)
        object_path.parent.mkdir(parents=True, exist_ok=True)
        if not object_path.exists():
            with object_path.open("xb") as stream:
                stream.write(body); stream.flush(); os.fsync(stream.fileno())
        _sync_directory(object_path.parent); _sync_directory(_roots(config)[0])
        revision = explanation["current_revision"] + 1; logical_path = _logical_object_path(digest)
        revision_value = {**source, "revision": revision, "object_sha256": digest,
                          "logical_path": logical_path, "created_at": _now(), "change_reason": "revised",
                          "revision_kind": "restore", "change_summary": f"Restored expression from revision {source_revision}",
                          "affected_question_ids": sorted(source["section_map"]),
                          "introduced_correction_ids": [], "restored_from_revision": source_revision}
        updated_explanation = {**explanation, "current_revision": revision,
                               "revisions": {**explanation["revisions"], str(revision): revision_value},
                               "confirmed_corrections": corrections}
        updated_questions = dict(record["questions"])
        for mapped_question_id, section_ids in source["section_map"].items():
            updated_questions[mapped_question_id] = {**updated_questions[mapped_question_id], "explanation_refs": [
                {"explanation_id": explanation["explanation_id"], "explanation_revision": revision,
                 "section_id": section_id, "object_sha256": digest, "logical_path": logical_path}
                for section_id in section_ids]}
        updated = {**record, "questions": updated_questions,
                   "explanations": {**record["explanations"], explanation["explanation_id"]: updated_explanation}}
        published = _publish(config, snapshot, updated, {digest: {"kind": "explanation_markdown", "size": len(body)}})
    public = {"explanation_id": explanation["explanation_id"], "root_question_id": root_id,
              **revision_value, "confirmed_corrections": corrections, "document_path": str(object_path)}
    return response(status="completed", workspace=str(config.config_path), result={"explanation": public,
                    "commit_id": published["commit_id"], "revision": published["revision"]},
                    artifact_refs=[str(object_path)], validation={"learning_record": "passed",
                    "corrections": "passed", "locations": "passed"})
