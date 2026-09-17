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
    "learning-record-v2.schema.json", "learning-snapshot-v2.schema.json"
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
        for frame in thread.get("return_route", []):
            framed_thread = threads.get(frame["thread_id"])
            framed_question = questions.get(frame["question_id"])
            if framed_thread is None or framed_thread["module_id"] != frame["module_id"] \
                    or framed_question is None or framed_question["thread_id"] != frame["thread_id"]:
                raise WorkspaceError(f"thread return route is invalid: {thread_id}")
        for entry in thread.get("entry_history", []):
            if any(questions.get(question_id, {}).get("thread_id") != thread_id
                   for question_id in (entry["from_question_id"], entry["to_question_id"])):
                raise WorkspaceError(f"thread entry history is invalid: {thread_id}")
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
    for question_id, question in questions.items():
        for ref in question["explanation_refs"]:
            explanation = explanations.get(ref["explanation_id"])
            revision = explanation and explanation["revisions"].get(str(ref["explanation_revision"]))
            if revision is None or ref["section_id"] not in revision["section_map"].get(question_id, []) \
                    or ref["object_sha256"] != revision["object_sha256"] \
                    or ref["logical_path"] != revision["logical_path"]:
                raise WorkspaceError(f"question explanation locator is invalid: {question_id}")


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
    atomic_write_json(path, {"schema_version": 2, "operation": operation,
                             "expected_revision": expected, "observed_revision": snapshot["revision"],
                             "proposal": proposal})
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
    return {"store": "learning-snapshot-v2", "commit_id": snapshot["commit_id"],
            "entries": [str(pointer), str(commits / f'{snapshot["commit_id"]}.json'),
                        *[str(objects / digest[:2] / digest) for digest in sorted(snapshot["objects"])]]}


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


def _thread_with_navigation(thread: dict[str, Any]) -> dict[str, Any]:
    """Materialize optional T08 fields without rewriting a valid T07 snapshot."""
    return {**thread, "return_route": list(thread.get("return_route", [])),
            "entry_history": list(thread.get("entry_history", []))}


def _question_state(record: dict[str, Any], question_id: str) -> dict[str, Any]:
    history = sorted((item for item in record["feedbacks"].values()
                      if item["question_id"] == question_id), key=lambda item: item["created_at"])
    question = record["questions"][question_id]
    return {"latest_feedback": history[-1] if history else None, "feedback_history": history,
            "unresolved_confusions": list(question["unresolved_confusions"])}


def _snapshot_at_revision(config: WorkspaceConfig, snapshot: dict[str, Any], revision: int) -> dict[str, Any] | None:
    if revision < 0 or revision > snapshot["revision"]:
        return None
    current = snapshot
    commits = _roots(config)[1]
    while current["revision"] > revision and current.get("parent_commit_id"):
        path = commits / f'{current["parent_commit_id"]}.json'
        if not path.is_file():
            return None
        current = read_json(path)
    return current if current["revision"] == revision else (_empty() if revision == 0 else None)


def _feedback_unchanged_since(config: WorkspaceConfig, snapshot: dict[str, Any], expected: int,
                              question_id: str) -> bool:
    previous = _snapshot_at_revision(config, snapshot, expected)
    if previous is None:
        return False
    return _question_state(previous["record"], question_id) == _question_state(snapshot["record"], question_id)


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
                  "current_question_id": question_id, "revision": 1, "created_at": _now(),
                  "return_route": [], "entry_history": []}
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
           text: str | None, expected_revision: int | None = None,
           existing_question_id: str | None = None, independent: bool = False) -> dict[str, Any]:
    if not existing_question_id and not (text or "").strip():
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
        if not existing_question_id and not independent:
            possible = [question_id for question_id, item in record["questions"].items()
                        if item["thread_id"] == thread_id and item["original_question"] == (text or "").strip()]
            if possible:
                return response(status="awaiting_user", workspace=str(config.config_path), result={
                    "possible_question_ids": possible, "submitted_question": (text or "").strip(),
                    "persisted": False}, validation={"question_identity": "ambiguous"},
                    next_action={"type": "user", "reason": "choose an existing question identity or confirm this is independent"})
        if existing_question_id:
            question = record["questions"].get(existing_question_id)
            if question is None or question["thread_id"] != thread_id:
                return response(status="missing_input", workspace=str(config.config_path),
                                diagnostics=["existing question is unknown or belongs to another thread"])
            question_id = existing_question_id
        else:
            question_id = "question-" + str(uuid.uuid4()); question = _question(question_id, thread_id, text or "")
        relationship_id = "relationship-" + str(uuid.uuid4())
        relationship = {"relationship_id": relationship_id, "thread_id": thread_id,
                        "from_question_id": from_question_id, "to_question_id": question_id,
                        "type": relation, "created_at": _now()}
        entry = {"entry_id": "entry-" + str(uuid.uuid4()), "from_question_id": from_question_id,
                 "to_question_id": question_id, "relation": relation, "created_at": _now()}
        navigable = _thread_with_navigation(thread)
        updated_thread = {**navigable, "current_question_id": question_id, "revision": thread["revision"] + 1,
                          "return_route": [*navigable["return_route"], {"module_id": thread["module_id"],
                              "thread_id": thread_id, "question_id": from_question_id, "entered_at": _now()}],
                          "entry_history": [*navigable["entry_history"], entry]}
        module_id = thread["module_id"]; module = record["modules"][module_id]
        updated = {**record,
                   "modules": {**record["modules"], module_id: {**module, "last_active_thread_id": thread_id}},
                   "threads": {**record["threads"], thread_id: updated_thread},
                   "questions": ({**record["questions"]} if existing_question_id else
                                 {**record["questions"], question_id: question}),
                   "relationships": {**record["relationships"], relationship_id: relationship}}
        published = _publish(config, snapshot, updated)
    return response(status="completed", workspace=str(config.config_path), result={"question": question,
                    "relationship": relationship, "entry": entry, "thread": updated_thread,
                    "revision": published["revision"], "commit_id": published["commit_id"]},
                    validation={"learning_record": "passed"})


def show_thread(config: WorkspaceConfig, thread_id: str) -> dict[str, Any]:
    snapshot = _load(config); record = snapshot["record"]; thread = record["threads"].get(thread_id)
    if thread is None:
        return response(status="missing_input", workspace=str(config.config_path), diagnostics=[f"unknown thread_id: {thread_id}"])
    question_ids = [key for key, value in record["questions"].items() if value["thread_id"] == thread_id]
    return response(status="completed", workspace=str(config.config_path), result={"thread": _thread_with_navigation(thread),
                    "questions": [value for value in record["questions"].values() if value["thread_id"] == thread_id],
                    "relationships": [value for value in record["relationships"].values() if value["thread_id"] == thread_id],
                    "question_states": {key: _question_state(record, key) for key in question_ids},
                    "revision": snapshot["revision"], "commit_id": snapshot["commit_id"]},
                    validation={"learning_record": "passed"})


def record_feedback(config: WorkspaceConfig, question_id: str, state: str, text: str,
                    confusion: str | None = None, expected_revision: int | None = None) -> dict[str, Any]:
    if state not in {"understood", "confused", "parked"} or not text.strip():
        return response(status="missing_input", workspace=str(config.config_path),
                        diagnostics=["feedback needs an explicit understood, confused, or parked state and original text"])
    with package_lock(_roots(config)[2].parent):
        snapshot = _load(config); record = snapshot["record"]; question = record["questions"].get(question_id)
        if question is None:
            return response(status="missing_input", workspace=str(config.config_path),
                            diagnostics=[f"unknown question_id: {question_id}"])
        if expected_revision is not None and expected_revision != snapshot["revision"] \
                and not _feedback_unchanged_since(config, snapshot, expected_revision, question_id):
            conflict = _revision_conflict(config, snapshot, expected_revision, "learning.feedback",
                                          {"question_id": question_id, "state": state,
                                           "original_text": text, "confusion": confusion})
            if conflict: return conflict
        feedback_id = "feedback-" + str(uuid.uuid4())
        feedback = {"feedback_id": feedback_id, "question_id": question_id, "state": state,
                    "original_text": text.strip(), "created_at": _now()}
        confusions = list(question["unresolved_confusions"])
        if confusion and confusion.strip() and confusion.strip() not in confusions:
            confusions.append(confusion.strip())
        updated_question = {**question, "unresolved_confusions": confusions}
        updated = {**record, "questions": {**record["questions"], question_id: updated_question},
                   "feedbacks": {**record["feedbacks"], feedback_id: feedback}}
        published = _publish(config, snapshot, updated)
    return response(status="completed", workspace=str(config.config_path), result={"feedback": feedback,
                    "question_state": _question_state(updated, question_id), "revision": published["revision"],
                    "commit_id": published["commit_id"]}, validation={"learning_record": "passed"})


def resume(config: WorkspaceConfig, thread_id: str | None, question_id: str | None = None,
           from_question_id: str | None = None, expected_revision: int | None = None,
           module_id: str | None = None) -> dict[str, Any]:
    snapshot = _load(config); record = snapshot["record"]
    if module_id:
        module = record["modules"].get(module_id)
        if module is None:
            return response(status="missing_input", workspace=str(config.config_path), diagnostics=[f"unknown module_id: {module_id}"])
        thread_id = module["last_active_thread_id"]
        if thread_id is None:
            return response(status="missing_input", workspace=str(config.config_path), diagnostics=["module has no learning thread to resume"])
    thread = record["threads"].get(thread_id or "")
    if thread is None:
        return response(status="missing_input", workspace=str(config.config_path), diagnostics=[f"unknown thread_id: {thread_id}"])
    current_state = _question_state(record, thread["current_question_id"])
    if question_id is None:
        status = "awaiting_user" if current_state["latest_feedback"] and current_state["latest_feedback"]["state"] == "parked" else "completed"
        return response(status=status, workspace=str(config.config_path), result={"thread": _thread_with_navigation(thread),
                        "question": record["questions"][thread["current_question_id"]], "question_state": current_state,
                        "choices": ([{"action": "continue", "question_id": thread["current_question_id"]},
                                     {"action": "back"}] if status == "awaiting_user" else [])},
                        next_action=({"type": "user", "reason": "choose whether to continue the parked question or return"}
                                     if status == "awaiting_user" else None))
    target = record["questions"].get(question_id)
    if target is None or target["thread_id"] != thread_id:
        return response(status="missing_input", workspace=str(config.config_path), diagnostics=["resume target must belong to thread"])
    origin_id = from_question_id or thread["current_question_id"]
    origin = record["questions"].get(origin_id)
    if origin is None:
        return response(status="missing_input", workspace=str(config.config_path), diagnostics=["resume origin is unknown"])
    origin_thread = record["threads"][origin["thread_id"]]
    with package_lock(_roots(config)[2].parent):
        snapshot = _load(config); record = snapshot["record"]; thread = record["threads"][thread_id]
        if expected_revision is not None:
            conflict = _revision_conflict(config, snapshot, expected_revision, "learning.resume",
                                          {"thread_id": thread_id, "question_id": question_id,
                                           "from_question_id": origin_id})
            if conflict: return conflict
        navigable = _thread_with_navigation(thread)
        updated_thread = {**navigable, "current_question_id": question_id, "revision": thread["revision"] + 1,
                          "return_route": [*navigable["return_route"], {"module_id": origin_thread["module_id"],
                              "thread_id": origin_thread["thread_id"], "question_id": origin_id, "entered_at": _now()}]}
        module = record["modules"][thread["module_id"]]
        updated = {**record, "threads": {**record["threads"], thread_id: updated_thread},
                   "modules": {**record["modules"], module["module_id"]: {**module, "last_active_thread_id": thread_id}}}
        published = _publish(config, snapshot, updated)
    return response(status="completed", workspace=str(config.config_path), result={"thread": updated_thread,
                    "question": target, "revision": published["revision"], "commit_id": published["commit_id"]})


def back(config: WorkspaceConfig, thread_id: str, expected_revision: int | None = None) -> dict[str, Any]:
    with package_lock(_roots(config)[2].parent):
        snapshot = _load(config); record = snapshot["record"]; thread = record["threads"].get(thread_id)
        if thread is None:
            return response(status="missing_input", workspace=str(config.config_path), diagnostics=[f"unknown thread_id: {thread_id}"])
        if expected_revision is not None:
            conflict = _revision_conflict(config, snapshot, expected_revision, "learning.back", {"thread_id": thread_id})
            if conflict: return conflict
        navigable = _thread_with_navigation(thread)
        if not navigable["return_route"]:
            return response(status="missing_input", workspace=str(config.config_path), diagnostics=["return route is empty"])
        frame = navigable["return_route"][-1]; origin_thread = record["threads"][frame["thread_id"]]
        popped_thread = {**navigable, "revision": thread["revision"] + 1,
                         "return_route": navigable["return_route"][:-1]}
        if origin_thread["thread_id"] == thread_id:
            returned_thread = {**popped_thread, "current_question_id": frame["question_id"]}
            threads = {**record["threads"], thread_id: returned_thread}
        else:
            returned_thread = {**_thread_with_navigation(origin_thread), "current_question_id": frame["question_id"],
                               "revision": origin_thread["revision"] + 1}
            threads = {**record["threads"], thread_id: popped_thread,
                       origin_thread["thread_id"]: returned_thread}
        origin_module = record["modules"][frame["module_id"]]
        updated = {**record, "threads": threads, "modules": {**record["modules"],
                   frame["module_id"]: {**origin_module, "last_active_thread_id": frame["thread_id"]}}}
        published = _publish(config, snapshot, updated)
    return response(status="completed", workspace=str(config.config_path), result={"thread": returned_thread,
                    "returned_to": frame, "revision": published["revision"], "commit_id": published["commit_id"]})
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


def commit_explanation(config: WorkspaceConfig, question_id: str, draft: Path, evidence_path: Path,
                       review_path: Path, profile: str, preparation_id: str,
                       expected_revision: int | None = None) -> dict[str, Any]:
    text = draft.read_text(encoding="utf-8")
    evidence = json.loads(evidence_path.read_text(encoding="utf-8")); review = json.loads(review_path.read_text(encoding="utf-8"))
    if profile not in PROFILES or not isinstance(evidence, list) or not evidence:
        return response(status="failed", workspace=str(config.config_path), diagnostics=["profile and at least one evidence reference are required"])
    markers = re.findall(r"<!--\s*section-id:\s*(section-[0-9a-f-]{36})\s*-->", text)
    errors = [] if len(markers) == 1 else ["draft must contain exactly one stable section-id marker"]
    errors.extend(_quality_errors(text, profile, review if isinstance(review, dict) else {}))
    if errors:
        return response(status="failed", workspace=str(config.config_path), validation={"teaching_quality": "failed"}, diagnostics=errors)
    body = text.encode(); digest = hashlib.sha256(body).hexdigest()
    with package_lock(_roots(config)[2].parent):
        snapshot = _load(config); record = snapshot["record"]; question = record["questions"].get(question_id)
        if expected_revision is not None:
            conflict = _revision_conflict(config, snapshot, expected_revision, "explanation.commit", {"question_id": question_id, "draft": str(draft)})
            if conflict: return conflict
        if question is None:
            return response(status="missing_input", workspace=str(config.config_path), diagnostics=[f"unknown question_id: {question_id}"])
        thread = record["threads"][question["thread_id"]]; module = record["modules"][thread["module_id"]]
        preparation = record["preparations"].get(preparation_id)
        scope_digest = hashlib.sha256(json.dumps(module["source_refs"], sort_keys=True).encode()).hexdigest()
        if preparation is None or preparation["question_id"] != question_id or preparation["profile"] != profile \
                or preparation["consumed_at"] is not None or preparation["source_refs"] != module["source_refs"] \
                or preparation["prepared_revision"] != snapshot["revision"] \
                or preparation["source_scope_sha256"] != scope_digest or markers != [preparation["section_id"]]:
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
        root_id = thread["root_question_id"]
        existing = next((item for item in record["explanations"].values() if item["root_question_id"] == root_id), None)
        explanation_id = existing["explanation_id"] if existing else "explanation-" + str(uuid.uuid4())
        revision = existing["current_revision"] + 1 if existing else 1
        object_root = _roots(config)[0]; object_path = _object_path(config, digest)
        object_path.parent.mkdir(parents=True, exist_ok=True)
        if not object_path.exists():
            with object_path.open("xb") as stream:
                stream.write(body); stream.flush(); os.fsync(stream.fileno())
        _sync_directory(object_path.parent); _sync_directory(object_root)
        logical_path = _logical_object_path(digest)
        revision_value = {"revision": revision, "object_sha256": digest, "logical_path": logical_path, "created_at": _now(),
                          "profile": profile, "evidence_refs": evidence, "teaching_review": review,
                          "section_map": {question_id: markers}, "change_reason": "initial" if not existing else "revised"}
        explanation = {"explanation_id": explanation_id, "root_question_id": root_id,
                       "current_revision": revision,
                       "revisions": {**(existing or {}).get("revisions", {}), str(revision): revision_value}}
        ref = {"explanation_id": explanation_id, "explanation_revision": revision,
               "section_id": markers[0], "object_sha256": digest, "logical_path": logical_path}
        updated_question = {**question, "explanation_refs": [ref]}
        updated = {**record, "questions": {**record["questions"], question_id: updated_question},
                   "preparations": {**record["preparations"], preparation_id: {**preparation, "consumed_at": _now()}},
                   "explanations": {**record["explanations"], explanation_id: explanation}}
        published = _publish(config, snapshot, updated, {digest: {"kind": "explanation_markdown", "size": len(body)}})
    public = {"explanation_id": explanation_id, "root_question_id": root_id, "revision": revision,
              **revision_value, "document_path": str(object_path)}
    return response(status="completed", workspace=str(config.config_path), result={"explanation": public,
                    "commit_id": published["commit_id"], "revision": published["revision"]},
                    artifact_refs=[str(object_path)], validation={"learning_record": "passed",
                    "teaching_quality": "passed", "source_versions": "passed"})
