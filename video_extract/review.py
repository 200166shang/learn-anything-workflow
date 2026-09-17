"""Active-recall preparations and immutable Review facts, independent from Learn."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from .command_response import engineering_revision, response
from .manifest import atomic_write_json, read_json
from .package_lock import package_lock
from .workspace import PORTABLE_SCHEMA_VERSION, WorkspaceConfig, WorkspaceError

SCHEMA_ROOT = Path(__file__).resolve().parent.parent / "schemas"
_SCHEMAS = {name: json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8")) for name in
            ("review-record-v1.schema.json", "review-snapshot-v1.schema.json",
             "review-current-pointer-v1.schema.json")}
_REGISTRY = Registry().with_resources((value["$id"], Resource.from_contents(value)) for value in _SCHEMAS.values())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate(name: str, value: Any) -> None:
    errors = list(Draft202012Validator(_SCHEMAS[name], registry=_REGISTRY,
                                       format_checker=FormatChecker()).iter_errors(value))
    if errors:
        raise WorkspaceError(f"{name} validation failed: {errors[0].message}")


def _roots(config: WorkspaceConfig) -> tuple[Path, Path]:
    if config.schema_version != PORTABLE_SCHEMA_VERSION or config.results is None:
        raise WorkspaceError("review-record v1 requires workspace schema v2")
    root = config.results / "review"
    if root.is_symlink() or root.resolve(strict=False) != config.results.resolve(strict=False) / "review":
        raise WorkspaceError(f"refusing symbolic link inside declared results root: {root}")
    commits, pointer = root / "commits", root / "current.json"
    if commits.is_symlink() or pointer.is_symlink():
        raise WorkspaceError("refusing symbolic link inside review store")
    return commits, pointer


def _empty() -> dict[str, Any]:
    return {"revision": 0, "commit_id": None, "record": {"schema_version": 1,
            "preparations": {}, "events": {}}}


def _load(config: WorkspaceConfig) -> dict[str, Any]:
    commits, pointer = _roots(config)
    if not pointer.is_file():
        return _empty()
    selected = read_json(pointer)
    _validate("review-current-pointer-v1.schema.json", selected)
    path = commits / f'{selected["commit_id"]}.json'
    if path.resolve(strict=False) != commits.resolve(strict=False) / f'{selected["commit_id"]}.json':
        raise WorkspaceError("review pointer resolves outside the commit store")
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != selected.get("manifest_sha256"):
        raise WorkspaceError("review snapshot manifest is missing or corrupt")
    snapshot = read_json(path)
    _validate("review-snapshot-v1.schema.json", snapshot)
    preparations = snapshot["record"]["preparations"]
    events = snapshot["record"]["events"]
    for preparation_id, preparation in preparations.items():
        if preparation["preparation_id"] != preparation_id:
            raise WorkspaceError(f"review preparation identity mismatch: {preparation_id}")
        consumed_by = preparation["consumed_by"]
        if consumed_by is not None:
            event = events.get(consumed_by)
            if event is None or event["preparation_id"] != preparation_id:
                raise WorkspaceError(f"review preparation has an invalid consumption link: {preparation_id}")
        _validate_pin(config, preparation["pin"])
    for event_id, event in events.items():
        preparation = preparations.get(event["preparation_id"])
        if event["event_id"] != event_id or preparation is None or preparation["consumed_by"] != event_id \
                or event["pin"] != preparation["pin"]:
            raise WorkspaceError(f"review event is not completely reachable: {event_id}")
    return snapshot


def _validate_pin(config: WorkspaceConfig, pin: dict[str, Any]) -> None:
    digest = pin["object_sha256"]
    logical_path = f"learning/objects/{digest[:2]}/{digest}"
    if pin["logical_path"] != logical_path:
        raise WorkspaceError("review pinned explanation path is not canonical")
    path = config.results / logical_path
    expected = config.results.resolve(strict=False) / logical_path
    if path.is_symlink() or path.resolve(strict=False) != expected:
        raise WorkspaceError("review pinned explanation path resolves outside the results root")
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise WorkspaceError("review pinned explanation object is missing or corrupt")


def _sync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def _publish(config: WorkspaceConfig, previous: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    commits, pointer = _roots(config)
    value = {"schema_version": 1, "parent_commit_id": previous.get("commit_id"),
             "revision": previous["revision"] + 1, "created_at": _now(), "record": record}
    stable = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    value["commit_id"] = "review-commit-" + hashlib.sha256(stable).hexdigest()
    _validate("review-snapshot-v1.schema.json", value)
    body = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"
    commits.mkdir(parents=True, exist_ok=True)
    path = commits / f'{value["commit_id"]}.json'
    if not path.exists():
        with path.open("xb") as stream:
            stream.write(body); stream.flush(); os.fsync(stream.fileno())
    _sync(commits)
    atomic_write_json(pointer, {"schema_version": 1, "commit_id": value["commit_id"],
                                "manifest_sha256": hashlib.sha256(body).hexdigest()})
    _sync(pointer.parent)
    return value


def _learning_pin(config: WorkspaceConfig, question_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    # Review only reads the authoritative Learn generation and never publishes it.
    from .learning import _load as load_learning
    snapshot = load_learning(config); record = snapshot["record"]
    question = record["questions"].get(question_id)
    if question is None or not question["explanation_refs"]:
        return None, {"question": question, "learning_commit_id": snapshot.get("commit_id")}
    ref = question["explanation_refs"][-1]
    explanation = record["explanations"][ref["explanation_id"]]
    revision = explanation["revisions"][str(ref["explanation_revision"])]
    source_refs = sorted({(item["source_id"], item["source_version"])
                          for item in revision["evidence_refs"]})
    return {"question_id": question_id, "explanation_id": ref["explanation_id"],
            "explanation_revision": ref["explanation_revision"], "object_sha256": ref["object_sha256"],
            "logical_path": ref["logical_path"],
            "source_refs": [{"source_id": source_id, "source_version": version}
                            for source_id, version in source_refs],
            "learning_commit_id": snapshot["commit_id"], "learning_revision": snapshot["revision"]}, question


def prepare(config: WorkspaceConfig, question_id: str, preparation_id: str | None = None) -> dict[str, Any]:
    pin, question = _learning_pin(config, question_id)
    if pin is None:
        return response(status="missing_input", workspace=str(config.config_path), result=question,
                        validation={"persisted_explanation": "failed"},
                        diagnostics=["review requires an existing question with a persisted explanation; no history was created"])
    with package_lock(_roots(config)[1].parent):
        snapshot = _load(config)
        preparation_id = preparation_id or "review-preparation-" + str(uuid.uuid4())
        if not preparation_id.startswith("review-preparation-"):
            return response(status="missing_input", workspace=str(config.config_path),
                            diagnostics=["preparation_id must start with review-preparation-"])
        existing = snapshot["record"]["preparations"].get(preparation_id)
        if existing is not None:
            if existing["pin"]["question_id"] != question_id:
                return response(status="awaiting_user", workspace=str(config.config_path),
                                validation={"preparation_id": "conflict"},
                                diagnostics=["preparation_id already identifies another question"])
            return response(status="awaiting_user", workspace=str(config.config_path), result={
                "preparation_id": preparation_id, "question_id": question_id,
                "prompt": existing["prompt"], "hint_policy": existing["hint_policy"],
                "answer_revealed": False, "replayed": True,
                "review_revision": snapshot["revision"], "review_commit_id": snapshot["commit_id"]},
                validation={"review_record": "passed", "answer_boundary": "passed",
                            "idempotency": "reused"},
                next_action={"type": "user", "reason": "answer from memory or explicitly skip"})
        item = {"preparation_id": preparation_id, "created_at": _now(),
                "prompt": question["original_question"],
                "hint_policy": ["Explain the causal mechanism in your own words.",
                                "If needed, request one minimal hint before revealing the explanation."],
                "pin": pin, "consumed_by": None}
        record = {**snapshot["record"], "preparations": {**snapshot["record"]["preparations"],
                                                           preparation_id: item}}
        published = _publish(config, snapshot, record)
    # Deliberately omit logical_path, object hash, and explanation text before recall.
    return response(status="awaiting_user", workspace=str(config.config_path), result={
        "preparation_id": preparation_id, "question_id": question_id, "prompt": item["prompt"],
        "hint_policy": item["hint_policy"], "answer_revealed": False,
        "review_revision": published["revision"], "review_commit_id": published["commit_id"]},
        validation={"review_record": "passed", "answer_boundary": "passed"},
        next_action={"type": "user", "reason": "answer from memory or explicitly skip"})


def record(config: WorkspaceConfig, preparation_id: str, event_id: str, *, answer: str | None,
           answer_summary: str | None, hints: list[str], model_evaluation: str,
           correction: str | None = None, corrected_evaluation: str | None = None) -> dict[str, Any]:
    allowed = {"recalled", "prompted", "not_recalled", "not_scored"}
    if model_evaluation not in allowed or corrected_evaluation not in allowed | {None}:
        return response(status="missing_input", workspace=str(config.config_path), diagnostics=["invalid review evaluation"])
    if model_evaluation != "not_scored" and not (answer or "").strip():
        return response(status="missing_input", workspace=str(config.config_path), diagnostics=["a scored review requires the user's actual answer"])
    if model_evaluation == "not_scored" and (answer or "").strip():
        return response(status="missing_input", workspace=str(config.config_path), diagnostics=["skipped or unanswered review must be not_scored"])
    if bool(correction) != bool(corrected_evaluation):
        return response(status="missing_input", workspace=str(config.config_path), diagnostics=["a correction requires both text and corrected evaluation"])
    normalized = {"preparation_id": preparation_id, "event_id": event_id,
                  "answer": answer.strip() if answer else None,
                  "answer_summary": answer_summary.strip() if answer_summary else None,
                  "hints": [item.strip() for item in hints if item.strip()],
                  "model_evaluation": model_evaluation,
                  "correction": correction.strip() if correction else None,
                  "corrected_evaluation": corrected_evaluation}
    request_sha256 = hashlib.sha256(json.dumps(normalized, ensure_ascii=False,
                                               sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with package_lock(_roots(config)[1].parent):
        snapshot = _load(config); record_value = snapshot["record"]
        existing = record_value["events"].get(event_id)
        if existing is not None:
            if existing["request_sha256"] != request_sha256:
                return response(status="awaiting_user", workspace=str(config.config_path),
                                validation={"event_id": "conflict"},
                                diagnostics=["event_id already identifies a different immutable Review fact"])
            return response(status="completed", workspace=str(config.config_path), result={
                "event": existing, "replayed": True, "reveal": _reveal(config, existing["pin"])},
                validation={"review_record": "passed", "idempotency": "reused"})
        preparation = record_value["preparations"].get(preparation_id)
        if preparation is None:
            return response(status="missing_input", workspace=str(config.config_path), diagnostics=["unknown review preparation"])
        if preparation["consumed_by"] is not None:
            return response(status="awaiting_user", workspace=str(config.config_path),
                            validation={"preparation": "consumed"}, diagnostics=["review preparation was already consumed"])
        corrections = ([{"text": correction.strip(), "corrected_evaluation": corrected_evaluation,
                         "created_at": _now()}] if correction else [])
        event = {"event_id": event_id, "preparation_id": preparation_id,
                 "request_sha256": request_sha256, "created_at": _now(),
                 "answer": normalized["answer"], "answer_summary": normalized["answer_summary"],
                 "hints": normalized["hints"],
                 "model_evaluation": model_evaluation,
                 "effective_evaluation": corrected_evaluation or model_evaluation,
                 "corrections": corrections, "pin": preparation["pin"],
                 "engine_version": engineering_revision(), "schema_version": 1}
        consumed = {**preparation, "consumed_by": event_id}
        updated = {**record_value,
                   "preparations": {**record_value["preparations"], preparation_id: consumed},
                   "events": {**record_value["events"], event_id: event}}
        published = _publish(config, snapshot, updated)
    return response(status="completed", workspace=str(config.config_path), result={
        "event": event, "replayed": False, "reveal": _reveal(config, event["pin"]),
        "review_revision": published["revision"], "review_commit_id": published["commit_id"]},
        validation={"review_record": "passed", "learning_record": "unchanged"})


def _reveal(config: WorkspaceConfig, pin: dict[str, Any]) -> dict[str, Any]:
    return {"explanation_id": pin["explanation_id"], "explanation_revision": pin["explanation_revision"],
            "object_sha256": pin["object_sha256"], "document_path": str(config.results / pin["logical_path"]),
            "source_refs": pin["source_refs"]}


def show(config: WorkspaceConfig, question_id: str | None = None) -> dict[str, Any]:
    snapshot = _load(config)
    events = list(snapshot["record"]["events"].values())
    if question_id:
        events = [item for item in events if item["pin"]["question_id"] == question_id]
    return response(status="completed", workspace=str(config.config_path), result={
        "events": sorted(events, key=lambda item: item["created_at"]), "review_schema_version": 1,
        "revision": snapshot["revision"], "commit_id": snapshot.get("commit_id")},
        validation={"review_record": "passed"})


def backup_entries(config: WorkspaceConfig) -> dict[str, Any]:
    snapshot = _load(config)
    if snapshot["revision"] == 0:
        return {"store": "review-snapshot-v1", "commit_id": None, "entries": []}
    commits, pointer = _roots(config)
    # _load has already checked the authority pointer, manifest digest, schema, and reachability.
    pins = [item["pin"] for item in snapshot["record"]["preparations"].values()]
    pinned_objects = sorted({str(config.results / pin["logical_path"]) for pin in pins})
    return {"store": "review-snapshot-v1", "commit_id": snapshot["commit_id"],
            "entries": [str(pointer), str(commits / f'{snapshot["commit_id"]}.json'),
                        *pinned_objects]}


def run_review(request: dict[str, Any]) -> dict[str, Any]:
    from .package_lock import PackageBusyError
    from .workspace import discover_workspace
    config = discover_workspace(Path(request["workspace"])); action = request.get("action")
    try:
        if action == "prepare": return prepare(config, request["question_id"], request.get("preparation_id"))
        if action == "record":
            return record(config, request["preparation_id"], request["event_id"], answer=request.get("answer"),
                          answer_summary=request.get("answer_summary"), hints=list(request.get("hints") or []),
                          model_evaluation=request["model_evaluation"], correction=request.get("correction"),
                          corrected_evaluation=request.get("corrected_evaluation"))
        if action == "show": return show(config, request.get("question_id"))
        return response(status="missing_input", workspace=str(config.config_path), diagnostics=["unsupported review action"])
    except PackageBusyError as exc:
        return response(status="busy", workspace=str(config.config_path), diagnostics=[str(exc)],
                        next_action={"type": "retry", "reason": "another Review write is in progress"})


run_review.__capability_contract__ = {"input_type": "review-request-v1", "output_type": "command-response-v1"}
