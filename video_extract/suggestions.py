"""Deterministic daily Review suggestions derived from authoritative facts."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .command_response import response
from .workspace import WorkspaceConfig


SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_SUGGESTIONS = 3


def _latest_by(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for item in sorted(items, key=lambda value: (value.get("created_at", ""), value.get("event_id", ""))):
        latest[item[key]] = item
    return latest


def _current_question_pin(record: dict[str, Any], question_id: str) -> dict[str, Any] | None:
    question = record["questions"].get(question_id)
    if question is None or not question["explanation_refs"]:
        return None
    return question["explanation_refs"][-1]


def _card_is_current(record: dict[str, Any], version: dict[str, Any]) -> bool:
    pin = version["explanation_pin"]
    current = _current_question_pin(record, pin["question_id"])
    return current is not None and all(current.get(key) == pin.get(key) for key in (
        "explanation_id", "explanation_revision", "object_sha256", "logical_path"
    ))


def today(config: WorkspaceConfig, on_date: str | None = None) -> dict[str, Any]:
    """Return at most three stable, read-only suggestions for one Shanghai date."""
    from .cards import _load as load_cards, schedule
    from .learning import _load as load_learning
    from .review import _load as load_review

    as_of = date.fromisoformat(on_date) if on_date else datetime.now(SHANGHAI).date()
    learning = load_learning(config)
    cards = load_cards(config)
    reviews = load_review(config)
    record = learning["record"]

    feedbacks = _latest_by(list(record["feedbacks"].values()), "question_id")
    latest_reviews = _latest_by([
        {**event, "question_id": event["pin"]["question_id"]}
        for event in reviews["record"]["events"].values()
        if event.get("review_date") is None or date.fromisoformat(event["review_date"]) <= as_of
    ], "question_id")

    ranked: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    card_questions: set[str] = set()
    invalid_cards = 0
    for card in cards["record"]["cards"].values():
        version = card["versions"][card["active_version_id"]]
        question_id = version["explanation_pin"]["question_id"]
        feedback = feedbacks.get(question_id)
        if feedback and feedback["state"] == "parked":
            continue
        if not _card_is_current(record, version):
            invalid_cards += 1
            continue
        computed = schedule(config, card_version_id=version["card_version_id"],
                            on_date=as_of.isoformat())["result"]
        if date.fromisoformat(computed["due_date"]) > as_of:
            continue
        card_questions.add(question_id)
        item = {
            "kind": "card_review",
            "object_id": card["card_id"],
            "object_version": version["card_version_id"],
            "question_id": question_id,
            "reason": f"卡片已于 {computed['due_date']} 到期，适合进行一次主动回忆",
            "estimated_minutes": 10,
            "action": {"command": "review prepare", "card_version_id": version["card_version_id"]},
            "due_date": computed["due_date"],
        }
        ranked.append(((0, computed["due_date"], card["card_id"]), item))

    for question_id, question in record["questions"].items():
        if question_id in card_questions or _current_question_pin(record, question_id) is None:
            continue
        thread = record["threads"].get(question["thread_id"])
        if thread is None:
            continue
        feedback = feedbacks.get(question_id)
        if feedback and feedback["state"] in {"understood", "parked"}:
            continue
        review = latest_reviews.get(question_id)
        difficult = bool(question["unresolved_confusions"]) or (feedback and feedback["state"] == "confused") \
            or (review and review["effective_evaluation"] in {"prompted", "not_recalled"})
        current_or_root = question_id in {thread["current_question_id"], thread["root_question_id"]}
        if not difficult and not current_or_root:
            continue
        if feedback and feedback["state"] == "confused":
            reason = "最近仍标记为不理解，建议用主动回忆定位具体卡点"
            recency = feedback["created_at"]
        elif question["unresolved_confusions"]:
            reason = "仍有未解决的困惑，建议先回忆再决定是否继续讲解"
            recency = question["created_at"]
        elif review and review["effective_evaluation"] in {"prompted", "not_recalled"}:
            reason = "最近一次回忆需要提示或未想起，适合短时强化"
            recency = review["created_at"]
        else:
            reason = "这是已开始主线上的当前问题，适合做一次短时主动回忆"
            recency = question["created_at"]
        pin = _current_question_pin(record, question_id)
        item = {
            "kind": "question_review",
            "object_id": question_id,
            "object_version": {
                "learning_commit_id": learning["commit_id"],
                "explanation_revision": pin["explanation_revision"],
                "object_sha256": pin["object_sha256"],
            },
            "question_id": question_id,
            "title": question["title"],
            "reason": reason,
            "estimated_minutes": 15,
            "action": {"command": "review prepare", "question_id": question_id},
        }
        recent_first = -datetime.fromisoformat(recency).timestamp()
        ranked.append(((1 if difficult else 2, recent_first, question_id), item))

    suggestions = [item for _, item in sorted(ranked, key=lambda pair: pair[0])[:MAX_SUGGESTIONS]]
    return response(
        status="completed",
        workspace=str(config.config_path),
        result={
            "as_of_date": as_of.isoformat(),
            "timezone": "Asia/Shanghai",
            "suggestions": suggestions,
            "empty_reason": None if suggestions else "no_eligible_started_learning_or_due_cards",
            "selection": {
                "choose": "run review prepare for the selected question_id or card_version_id",
                "change": "any other valid question_id or card_version_id may be prepared instead",
                "skip": "do nothing; no Learn or Review fact is written",
                "long_practice": "available only when explicitly chosen and may exceed 15 minutes",
            },
            "source_revisions": {
                "learning_commit_id": learning.get("commit_id"),
                "card_commit_id": cards.get("commit_id"),
                "review_commit_id": reviews.get("commit_id"),
            },
        },
        validation={
            "learning_record": "passed",
            "card_record": "passed",
            "review_record": "passed",
            "invalid_cards_excluded": invalid_cards,
            "recommendation_persisted": False,
        },
    )


def run_suggestions(request: dict[str, Any]) -> dict[str, Any]:
    from .workspace import discover_workspace
    config = discover_workspace(Path(request["workspace"]))
    if request.get("action") != "today":
        return response(status="missing_input", workspace=str(config.config_path),
                        diagnostics=["unsupported suggestions action"])
    return today(config, request.get("on_date"))


run_suggestions.__capability_contract__ = {
    "input_type": "suggestions-request-v1", "output_type": "command-response-v1"
}
