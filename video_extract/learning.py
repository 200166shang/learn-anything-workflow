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
                                              "questions": {}, "relationships": {},
                                              "explanations": {}}, "objects": {}}


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
    for explanation in value["record"]["explanations"].values():
        for revision in explanation["revisions"].values():
            if revision["object_sha256"] not in value["objects"]:
                raise WorkspaceError("explanation revision is not reachable through learning snapshot objects")
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
    candidates = _roots(config)[3]; candidates.mkdir(parents=True, exist_ok=True)
    path = candidates / f"candidate-{uuid.uuid4()}.json"
    atomic_write_json(path, {"schema_version": 2, "operation": operation,
                             "expected_revision": expected, "observed_revision": snapshot["revision"],
                             "proposal": proposal})
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


def create_module(config: WorkspaceConfig, goal: str, scope: str, source_id: str,
                  source_version: str, expected_revision: int | None = None) -> dict[str, Any]:
    if not goal.strip() or not scope.strip():
        return response(status="missing_input", workspace=str(config.config_path),
                        diagnostics=["confirmed module goal and scope must be non-empty"])
    checked = verify_source(config, source_id, source_version)
    if checked["status"] != "completed":
        return checked
    module_id = "module-" + str(uuid.uuid4())
    with package_lock(_roots(config)[2].parent):
        snapshot = _load(config)
        if expected_revision is not None:
            conflict = _revision_conflict(config, snapshot, expected_revision, "module.create", {"goal": goal, "scope": scope})
            if conflict: return conflict
        module = {"module_id": module_id, "goal": goal.strip(), "scope": scope.strip(),
                  "source_refs": [{"source_id": source_id, "source_version": source_version}],
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
    return response(status="completed", workspace=str(config.config_path), result={"module": module,
                    "learning_schema_version": 2, "revision": snapshot["revision"],
                    "commit_id": snapshot["commit_id"]}, validation={"learning_record": "passed"})


def recommend_roots(config: WorkspaceConfig, module_id: str) -> dict[str, Any]:
    snapshot = _load(config)
    module = snapshot["record"]["modules"].get(module_id)
    if module is None:
        return response(status="missing_input", workspace=str(config.config_path),
                        diagnostics=[f"unknown module_id: {module_id}"])
    context = []
    for ref in module["source_refs"]:
        checked = verify_source(config, ref["source_id"], ref["source_version"])
        if checked["status"] == "completed":
            context.append({**ref, "kind": checked["result"]["kind"],
                            "title": checked["result"]["title"],
                            "content": checked["result"].get("content"),
                            "location": checked["result"].get("location"),
                            "availability": checked["result"].get("availability")})
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


def locate(config: WorkspaceConfig, question_id: str) -> dict[str, Any]:
    snapshot = _load(config); question = snapshot["record"]["questions"].get(question_id)
    if question is None:
        return response(status="missing_input", workspace=str(config.config_path), diagnostics=[f"unknown question_id: {question_id}"])
    if not question["explanation_refs"]:
        return response(status="awaiting_model", workspace=str(config.config_path), result={
            "question": question, "explanation_state": "pending"},
            next_action={"type": "model", "action": "prepare_explanation"})
    return response(status="completed", workspace=str(config.config_path), result={
        "question": question, "explanation_state": "available", "locations": question["explanation_refs"]})


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
    context = []
    for ref in module["source_refs"]:
        checked = verify_source(config, ref["source_id"], ref["source_version"])
        if checked["status"] == "completed":
            context.append({**ref, "kind": checked["result"]["kind"], "title": checked["result"]["title"],
                            "content": checked["result"].get("content"),
                            "location": checked["result"].get("location"),
                            "availability": checked["result"].get("availability"),
                            "version_basis": checked["result"]["version_basis"]})
    unavailable = [item for item in context if item["availability"] not in {"available_from_results", "available_at_location"}]
    if unavailable:
        return response(status="missing_input", workspace=str(config.config_path), result={
            "question": question, "blocked_source_refs": unavailable},
            diagnostics=["required source version is unavailable for this question"],
            next_action={"type": "user", "reason": "relocate or restore the required source version"})
    section_id = "section-" + str(uuid.uuid4())
    return response(status="awaiting_model", workspace=str(config.config_path), result={
        "question": question, "profile": profile, "section_id": section_id,
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
                       review_path: Path, profile: str, expected_revision: int | None = None) -> dict[str, Any]:
    text = draft.read_text(encoding="utf-8")
    evidence = json.loads(evidence_path.read_text(encoding="utf-8")); review = json.loads(review_path.read_text(encoding="utf-8"))
    if profile not in PROFILES or not isinstance(evidence, list) or not evidence:
        return response(status="failed", workspace=str(config.config_path), diagnostics=["profile and at least one evidence reference are required"])
    markers = re.findall(r"<!--\s*section-id:\s*(section-[0-9a-f-]{36})\s*-->", text)
    errors = [] if len(markers) == 1 else ["draft must contain exactly one stable section-id marker"]
    errors.extend(_quality_errors(text, profile, review if isinstance(review, dict) else {}))
    for item in evidence:
        if (not isinstance(item, dict)
                or item.get("claim_type") not in {"course_fact", "current_code", "supplemental_source", "inference"}
                or not item.get("source_id") or not item.get("source_version")
                or not item.get("locator") or not item.get("claim")):
            errors.append("each evidence reference needs source identity, locator, claim, and claim_type"); continue
        checked = verify_source(config, item.get("source_id", ""), item.get("source_version"))
        if checked["status"] != "completed": errors.append("evidence source version is unavailable")
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
        thread = record["threads"][question["thread_id"]]; root_id = thread["root_question_id"]
        existing = next((item for item in record["explanations"].values() if item["root_question_id"] == root_id), None)
        explanation_id = existing["explanation_id"] if existing else "explanation-" + str(uuid.uuid4())
        revision = existing["current_revision"] + 1 if existing else 1
        object_root = _roots(config)[0]; object_path = _object_path(config, digest)
        object_path.parent.mkdir(parents=True, exist_ok=True)
        if not object_path.exists():
            with object_path.open("xb") as stream:
                stream.write(body); stream.flush(); os.fsync(stream.fileno())
        _sync_directory(object_path.parent); _sync_directory(object_root)
        revision_value = {"revision": revision, "object_sha256": digest, "created_at": _now(),
                          "profile": profile, "evidence_refs": evidence, "teaching_review": review,
                          "section_map": {question_id: markers}, "change_reason": "initial" if not existing else "revised"}
        explanation = {"explanation_id": explanation_id, "root_question_id": root_id,
                       "current_revision": revision,
                       "revisions": {**(existing or {}).get("revisions", {}), str(revision): revision_value}}
        ref = {"explanation_id": explanation_id, "explanation_revision": revision,
               "section_id": markers[0], "document_path": str(object_path)}
        updated_question = {**question, "explanation_refs": [ref]}
        updated = {**record, "questions": {**record["questions"], question_id: updated_question},
                   "explanations": {**record["explanations"], explanation_id: explanation}}
        published = _publish(config, snapshot, updated, {digest: {"kind": "explanation_markdown", "size": len(body)}})
    public = {"explanation_id": explanation_id, "root_question_id": root_id, "revision": revision,
              **revision_value, "document_path": str(object_path)}
    return response(status="completed", workspace=str(config.config_path), result={"explanation": public,
                    "commit_id": published["commit_id"], "revision": published["revision"]},
                    artifact_refs=[str(object_path)], validation={"learning_record": "passed",
                    "teaching_quality": "passed", "source_versions": "passed"})
