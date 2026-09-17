"""Selected flash cards, immutable versions, and rule-v1 schedules."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator, FormatChecker

from .command_response import engineering_revision, response
from .manifest import atomic_write_json, read_json
from .package_lock import package_lock
from .workspace import PORTABLE_SCHEMA_VERSION, WorkspaceConfig, WorkspaceError

RULE_VERSION = 1
SHANGHAI = ZoneInfo("Asia/Shanghai")
_SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "schemas/card-record-v1.schema.json").read_text())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _roots(config: WorkspaceConfig) -> tuple[Path, Path]:
    if config.schema_version != PORTABLE_SCHEMA_VERSION or config.results is None:
        raise WorkspaceError("card v1 requires workspace schema v2")
    root = config.results / "cards"
    if root.is_symlink() or root.resolve(strict=False) != config.results.resolve(strict=False) / "cards":
        raise WorkspaceError("card store resolves outside the results root")
    return root / "commits", root / "current.json"


def _empty() -> dict[str, Any]:
    return {"schema_version": 1, "revision": 0, "commit_id": None,
            "record": {"schema_version": 1, "cards": {}}}


def _load(config: WorkspaceConfig) -> dict[str, Any]:
    commits, pointer = _roots(config)
    if not pointer.is_file():
        return _empty()
    selected = read_json(pointer)
    commit_id = selected.get("commit_id")
    path = commits / f"{commit_id}.json"
    if not isinstance(commit_id, str) or not path.is_file() \
            or hashlib.sha256(path.read_bytes()).hexdigest() != selected.get("manifest_sha256"):
        raise WorkspaceError("card snapshot manifest is missing or corrupt")
    snapshot = read_json(path)
    if snapshot.get("commit_id") != commit_id or snapshot.get("schema_version") != 1:
        raise WorkspaceError("card snapshot identity or schema is invalid")
    _validate_record(config, snapshot.get("record"))
    return snapshot


def _validate_record(config: WorkspaceConfig, record: Any) -> None:
    errors = list(Draft202012Validator(_SCHEMA, format_checker=FormatChecker()).iter_errors(record))
    if errors:
        raise WorkspaceError(f"card-record-v1 validation failed: {errors[0].message}")
    for card_id, card in record["cards"].items():
        if card.get("card_id") != card_id or not card.get("versions") \
                or card.get("active_version_id") not in card["versions"]:
            raise WorkspaceError(f"card history is invalid: {card_id}")
        numbers = []
        for version_id, version in card["versions"].items():
            if version["card_version_id"] != version_id:
                raise WorkspaceError(f"card version identity mismatch: {version_id}")
            numbers.append(version["version_number"])
            pin = version["explanation_pin"]
            path = config.results / pin["logical_path"]
            if path.is_symlink() or path.resolve(strict=False) != config.results.resolve(strict=False) / pin["logical_path"] \
                    or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != pin["object_sha256"]:
                raise WorkspaceError(f"card explanation pin is missing or corrupt: {version_id}")
        if sorted(numbers) != list(range(1, len(numbers) + 1)) or len(numbers) != len(set(numbers)):
            raise WorkspaceError(f"card version sequence is invalid: {card_id}")
        if not set(card["question_ids"]).issuperset(
                event["question_id"] for event in card["selection_events"]):
            raise WorkspaceError(f"card selection has an unlinked question: {card_id}")


def _sync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish(config: WorkspaceConfig, previous: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    commits, pointer = _roots(config)
    _validate_record(config, record)
    value = {"schema_version": 1, "parent_commit_id": previous.get("commit_id"),
             "revision": previous["revision"] + 1, "created_at": _now(), "record": record}
    stable = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    value["commit_id"] = "card-commit-" + hashlib.sha256(stable).hexdigest()
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


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def _candidate_id(candidate: dict[str, Any]) -> str:
    facts = {key: value for key, value in candidate.items() if key != "candidate_id"}
    return "card-candidate-" + hashlib.sha256(json.dumps(
        facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def propose(config: WorkspaceConfig, question_id: str, memory_target: str, conditions: str,
            prompt: str, answer: str, reason: str) -> dict[str, Any]:
    from .review import _learning_pin
    pin, question = _learning_pin(config, question_id)
    if pin is None:
        return response(status="missing_input", workspace=str(config.config_path), result=question,
                        diagnostics=["card proposal requires a persisted explanation"])
    snapshot = _load(config)
    match = next((card for card in snapshot["record"]["cards"].values()
                  if _normalized(card["memory_target"]) == _normalized(memory_target)
                  and _normalized(card["conditions"]) == _normalized(conditions)), None)
    candidate = {"schema_version": 1, "proposal": "reuse" if match else "new",
                 "existing_card_id": match["card_id"] if match else None,
                 "question_id": question_id, "memory_target": memory_target.strip(),
                 "conditions": conditions.strip(), "prompt": prompt.strip(), "answer": answer.strip(),
                 "reason": reason.strip(), "explanation_pin": pin, "rule_version": RULE_VERSION}
    candidate["candidate_id"] = _candidate_id(candidate)
    return response(status="completed", workspace=str(config.config_path),
                    result={"candidate": candidate, "persisted": False},
                    validation={"learning_question": "passed", "selection": "required"})


def select(config: WorkspaceConfig, candidate_path: Path, effective_date: str | None = None) -> dict[str, Any]:
    candidate = read_json(candidate_path)
    if candidate.get("candidate_id") != _candidate_id(candidate):
        return response(status="missing_input", workspace=str(config.config_path),
                        diagnostics=["card candidate identity does not match its facts"])
    selected_date = date.fromisoformat(effective_date) if effective_date else datetime.now(SHANGHAI).date()
    with package_lock(_roots(config)[1].parent):
        snapshot = _load(config); cards = snapshot["record"]["cards"]
        replayed = next((card for card in cards.values()
                         if any(event["candidate_id"] == candidate["candidate_id"]
                                for event in card["selection_events"])), None)
        if replayed is not None:
            return response(status="completed", workspace=str(config.config_path),
                            result={"card": replayed, "card_revision": snapshot["revision"],
                                    "replayed": True},
                            validation={"selection": "passed", "idempotency": "reused"})
        existing_id = candidate.get("existing_card_id")
        if existing_id is None:
            matching = next((item for item in cards.values()
                             if _normalized(item["memory_target"]) == _normalized(candidate["memory_target"])
                             and _normalized(item["conditions"]) == _normalized(candidate["conditions"])), None)
            existing_id = matching["card_id"] if matching else None
        if existing_id:
            card = cards.get(existing_id)
            if card is None or _normalized(card["memory_target"]) != _normalized(candidate["memory_target"]) \
                    or _normalized(card["conditions"]) != _normalized(candidate["conditions"]):
                return response(status="awaiting_user", workspace=str(config.config_path),
                                diagnostics=["the proposed reusable card changed; propose again"])
            questions = sorted(set(card["question_ids"]) | {candidate["question_id"]})
            card = {**card, "question_ids": questions,
                    "selection_events": [*card["selection_events"], {
                        "candidate_id": candidate["candidate_id"], "question_id": candidate["question_id"],
                        "selected_at": _now()}]}
        else:
            card_id = "card-" + str(uuid.uuid4()); version_id = "card-version-" + str(uuid.uuid4())
            version = {"card_version_id": version_id, "version_number": 1,
                       "prompt": candidate["prompt"], "answer": candidate["answer"],
                       "conditions": candidate["conditions"], "effective_date": selected_date.isoformat(),
                       "created_at": _now(), "change_type": "selected", "rule_version": RULE_VERSION,
                       "explanation_pin": candidate["explanation_pin"], "revisions": []}
            card = {"card_id": card_id, "memory_target": candidate["memory_target"],
                    "conditions": candidate["conditions"], "question_ids": [candidate["question_id"]],
                    "active_version_id": version_id, "versions": {version_id: version},
                    "selection_events": [{"candidate_id": candidate["candidate_id"],
                                          "question_id": candidate["question_id"], "selected_at": _now()}]}
        updated = {**cards, card["card_id"]: card}
        published = _publish(config, snapshot, {"schema_version": 1, "cards": updated})
    return response(status="completed", workspace=str(config.config_path),
                    result={"card": card, "card_revision": published["revision"]},
                    validation={"selection": "passed", "card_record": "passed"})


def _find_version(snapshot: dict[str, Any], version_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    for card in snapshot["record"]["cards"].values():
        if version_id in card["versions"]:
            return card, card["versions"][version_id]
    raise WorkspaceError(f"unknown card version: {version_id}")


def get_version(config: WorkspaceConfig, version_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    return _find_version(_load(config), version_id)


def show(config: WorkspaceConfig, card_id: str | None = None) -> dict[str, Any]:
    snapshot = _load(config)
    cards = snapshot["record"]["cards"]
    if card_id is not None:
        card = cards.get(card_id)
        if card is None:
            return response(status="missing_input", workspace=str(config.config_path),
                            diagnostics=[f"unknown card: {card_id}"])
        cards = {card_id: card}
    return response(status="completed", workspace=str(config.config_path),
                    result={"cards": cards, "revision": snapshot["revision"],
                            "commit_id": snapshot.get("commit_id"), "card_schema_version": 1},
                    validation={"card_record": "passed", "history": "passed"})


def revise(config: WorkspaceConfig, card_id: str, change_type: str, *, prompt: str | None,
           answer: str | None, conditions: str | None, effective_date: str) -> dict[str, Any]:
    chosen_date = date.fromisoformat(effective_date)
    with package_lock(_roots(config)[1].parent):
        snapshot = _load(config); card = snapshot["record"]["cards"].get(card_id)
        if card is None:
            return response(status="missing_input", workspace=str(config.config_path), diagnostics=["unknown card"])
        current = card["versions"][card["active_version_id"]]
        from .review import _learning_pin
        latest_pin, _ = _learning_pin(config, current["explanation_pin"]["question_id"])
        if latest_pin is None:
            return response(status="missing_input", workspace=str(config.config_path),
                            diagnostics=["the card question no longer has a valid explanation"])
        if change_type == "wording":
            revision = {"revised_at": _now(), "prompt": prompt or current["prompt"],
                        "answer": answer or current["answer"], "reason": "wording"}
            version = {**current, "prompt": prompt or current["prompt"],
                       "answer": answer or current["answer"],
                       "explanation_pin": latest_pin,
                       "revisions": [*current["revisions"], revision]}
            versions = {**card["versions"], current["card_version_id"]: version}
            updated_card = {**card, "versions": versions}
        else:
            version_id = "card-version-" + str(uuid.uuid4())
            version = {"card_version_id": version_id, "version_number": current["version_number"] + 1,
                       "prompt": prompt or current["prompt"], "answer": answer or current["answer"],
                       "conditions": conditions or current["conditions"], "effective_date": chosen_date.isoformat(),
                       "created_at": _now(), "change_type": "material", "rule_version": RULE_VERSION,
                       "explanation_pin": latest_pin, "revisions": []}
            updated_card = {**card, "conditions": version["conditions"], "active_version_id": version_id,
                            "versions": {**card["versions"], version_id: version}}
        cards = {**snapshot["record"]["cards"], card_id: updated_card}
        published = _publish(config, snapshot, {"schema_version": 1, "cards": cards})
    computed = schedule(config, card_version_id=version["card_version_id"], on_date=chosen_date.isoformat())
    return response(status="completed", workspace=str(config.config_path),
                    result={"card": updated_card, "version": version, "schedule": computed["result"],
                            "card_revision": published["revision"]},
                    validation={"card_record": "passed", "history": "preserved"})


def schedule(config: WorkspaceConfig, *, card_id: str | None = None,
             card_version_id: str | None = None, on_date: str | None = None) -> dict[str, Any]:
    snapshot = _load(config)
    if card_id:
        card = snapshot["record"]["cards"].get(card_id)
        if card is None: raise WorkspaceError(f"unknown card: {card_id}")
        card_version_id = card["active_version_id"]
        version = card["versions"][card_version_id]
    elif card_version_id:
        card, version = _find_version(snapshot, card_version_id)
    else:
        raise WorkspaceError("card_id or card_version_id is required")
    from .review import _load as load_review
    as_of = date.fromisoformat(on_date) if on_date else datetime.now(SHANGHAI).date()
    events = [event for event in load_review(config)["record"]["events"].values()
              if event["pin"].get("card_version_id") == card_version_id
              and event["effective_evaluation"] != "not_scored"
              and date.fromisoformat(event.get("review_date") or event["created_at"][:10]) <= as_of]
    events.sort(key=lambda item: (item.get("review_date", item["created_at"][:10]), item["created_at"], item["event_id"]))
    streak = 0; due = date.fromisoformat(version["effective_date"]) + timedelta(days=1)
    used = []
    intervals = (3, 7, 14, 30)
    for event in events:
        review_day = date.fromisoformat(event.get("review_date", event["created_at"][:10]))
        used.append(event["event_id"])
        if event["effective_evaluation"] == "recalled":
            interval = intervals[min(streak, len(intervals) - 1)]; streak += 1
        else:
            interval = 1; streak = 0
        due = review_day + timedelta(days=interval)
    result = {"card_id": card["card_id"], "card_version_id": card_version_id,
              "as_of_date": as_of.isoformat(),
              "due_date": due.isoformat(), "rule_version": RULE_VERSION,
              "independent_streak": streak, "derived_from_event_ids": used,
              "engine_version": engineering_revision()}
    return response(status="completed", workspace=str(config.config_path), result=result,
                    validation={"card_record": "passed", "review_facts": "passed", "cache": "derived"})


def backup_entries(config: WorkspaceConfig) -> dict[str, Any]:
    snapshot = _load(config)
    if snapshot["revision"] == 0:
        return {"store": "card-snapshot-v1", "commit_id": None, "entries": []}
    commits, pointer = _roots(config)
    return {"store": "card-snapshot-v1", "commit_id": snapshot["commit_id"],
            "entries": [str(pointer), str(commits / f'{snapshot["commit_id"]}.json')]}


def run_cards(request: dict[str, Any]) -> dict[str, Any]:
    from .workspace import discover_workspace
    config = discover_workspace(Path(request["workspace"])); action = request.get("action")
    if action == "propose":
        return propose(config, request["question_id"], request["memory_target"], request["conditions"],
                       request["prompt"], request["answer"], request["reason"])
    if action == "select":
        return select(config, Path(request["candidate"]), request.get("effective_date"))
    if action == "revise":
        return revise(config, request["card_id"], request["change_type"], prompt=request.get("prompt"),
                      answer=request.get("answer"), conditions=request.get("conditions"),
                      effective_date=request["effective_date"])
    if action == "schedule":
        return schedule(config, card_id=request.get("card_id"),
                        card_version_id=request.get("card_version_id"), on_date=request.get("on_date"))
    if action == "show":
        return show(config, request.get("card_id"))
    return response(status="missing_input", workspace=str(config.config_path), diagnostics=["unsupported card action"])


run_cards.__capability_contract__ = {"input_type": "card-request-v1", "output_type": "command-response-v1"}
