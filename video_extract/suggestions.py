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
MAX_TOTAL_MINUTES = 15


def _normalized(value: str) -> str:
    return " ".join(value.split()).casefold()


def _card_candidates(learning: dict[str, Any], cards: dict[str, Any], as_of: date,
                     preferred_versions: set[str]) -> tuple[list[tuple[tuple[Any, ...], dict[str, Any]]], int]:
    ranked: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    invalid = 0; questions = learning["questions"]
    for card in cards["cards"]:
        version = card["version"]; pin = version["explanation_pin"]
        question = questions.get(pin["question_id"])
        preferred = version["card_version_id"] in preferred_versions
        current = question and question["explanation_pin"]
        if current is None or any(current.get(key) != pin.get(key) for key in (
                "explanation_id", "explanation_revision", "object_sha256", "logical_path")):
            invalid += 1; continue
        feedback = question["latest_feedback"]
        if feedback and feedback["state"] == "parked" and not preferred:
            continue
        due_date = card["schedule"]["due_date"]
        if date.fromisoformat(due_date) > as_of and not preferred:
            continue
        semantic_key = f"{_normalized(card['memory_target'])}\0{_normalized(card['conditions'])}"
        item = {
            "kind": "card_review", "object_id": card["card_id"],
            "object_version": version["card_version_id"], "question_id": pin["question_id"],
            "reason": ("你明确选择今天强化这张卡片" if preferred else
                       f"卡片已于 {due_date} 到期，适合进行一次主动回忆"),
            "estimated_minutes": 5,
            "semantic_context": {"memory_target": card["memory_target"],
                                 "conditions": card["conditions"],
                                 "module_goal": question["module_goal"],
                                 "module_scope": question["module_scope"]},
            "action": {"command": "review prepare", "card_version_id": version["card_version_id"]},
            "due_date": due_date,
        }
        ranked.append(((-1 if preferred else 0, due_date, semantic_key, card["card_id"]), item))
    return ranked, invalid


def _question_candidates(learning: dict[str, Any], reviews: dict[str, Any], as_of: date,
                         preferred_questions: set[str], card_questions: set[str]
                         ) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    ranked: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for question_id, question in learning["questions"].items():
        if question_id in card_questions or question["explanation_pin"] is None:
            continue
        preferred = question_id in preferred_questions; feedback = question["latest_feedback"]
        if feedback and feedback["state"] in {"understood", "parked"} and not preferred:
            continue
        review = reviews["latest_by_question"].get(question_id)
        if review and date.fromisoformat(review["review_day"]) == as_of and not preferred:
            continue
        difficult = bool(question["unresolved_confusions"]) or (feedback and feedback["state"] == "confused") \
            or (review and review["effective_evaluation"] in {"prompted", "not_recalled"})
        if not preferred and not difficult and not (question["is_current"] or question["is_root"]):
            continue
        if preferred:
            reason, recency = "你明确选择今天强化这个已学问题", question["created_at"]
        elif feedback and feedback["state"] == "confused":
            reason, recency = "最近仍标记为不理解，建议用主动回忆定位具体卡点", feedback["created_at"]
        elif question["unresolved_confusions"]:
            reason, recency = "仍有未解决的困惑，建议先回忆再决定是否继续讲解", question["created_at"]
        elif review and review["effective_evaluation"] in {"prompted", "not_recalled"}:
            reason, recency = "最近一次回忆需要提示或未想起，适合短时强化", review["created_at"]
        else:
            reason, recency = "这是已开始主线上的当前问题，适合做一次短时主动回忆", question["created_at"]
        pin = question["explanation_pin"]
        item = {
            "kind": "question_review", "object_id": question_id,
            "object_version": {"learning_commit_id": learning["commit_id"],
                               "explanation_revision": pin["explanation_revision"],
                               "object_sha256": pin["object_sha256"]},
            "question_id": question_id, "title": question["title"], "reason": reason,
            "estimated_minutes": 10,
            "semantic_context": {"module_goal": question["module_goal"],
                                 "module_scope": question["module_scope"],
                                 "unresolved_confusions": question["unresolved_confusions"],
                                 "feedback_state": feedback["state"] if feedback else None},
            "action": {"command": "review prepare", "question_id": question_id},
        }
        semantic_key = (_normalized(question["module_goal"]), _normalized(question["module_scope"]),
                        _normalized(question["title"]))
        ranked.append(((-1 if preferred else 1 if difficult else 2,
                        -datetime.fromisoformat(recency).timestamp(), semantic_key, question_id), item))
    return ranked


def _within_budget(ranked: list[tuple[tuple[Any, ...], dict[str, Any]]]) -> tuple[list[dict[str, Any]], int]:
    suggestions: list[dict[str, Any]] = []; minutes = 0; seen: set[tuple[str, str]] = set()
    for _, item in sorted(ranked, key=lambda pair: pair[0]):
        if len(suggestions) >= MAX_SUGGESTIONS:
            break
        dedupe = (item["kind"], repr(item["semantic_context"]))
        if dedupe in seen or minutes + item["estimated_minutes"] > MAX_TOTAL_MINUTES:
            continue
        seen.add(dedupe); suggestions.append(item); minutes += item["estimated_minutes"]
    return suggestions, minutes


def today(config: WorkspaceConfig, on_date: str | None = None, *,
          preferred_question_ids: list[str] | None = None,
          preferred_card_version_ids: list[str] | None = None) -> dict[str, Any]:
    """Return a stable, read-only short Review group for one Shanghai date."""
    from .cards import suggestion_facts as card_facts
    from .learning import suggestion_facts as learning_facts
    from .review import suggestion_facts as review_facts

    as_of = date.fromisoformat(on_date) if on_date else datetime.now(SHANGHAI).date()
    learning = learning_facts(config); cards = card_facts(config, as_of.isoformat())
    reviews = review_facts(config, as_of.isoformat())
    card_ranked, invalid_cards = _card_candidates(
        learning, cards, as_of, set(preferred_card_version_ids or []))
    card_questions = {item["question_id"] for _, item in card_ranked}
    question_ranked = _question_candidates(
        learning, reviews, as_of, set(preferred_question_ids or []), card_questions)
    suggestions, total_minutes = _within_budget([*card_ranked, *question_ranked])
    return response(status="completed", workspace=str(config.config_path), result={
        "as_of_date": as_of.isoformat(), "timezone": "Asia/Shanghai",
        "suggestions": suggestions, "total_estimated_minutes": total_minutes,
        "maximum_total_minutes": MAX_TOTAL_MINUTES,
        "empty_reason": None if suggestions else "no_eligible_started_learning_or_due_cards",
        "selection": {
            "choose": "run review prepare for the selected question_id or card_version_id",
            "change": "any other valid question_id or card_version_id may be prepared instead",
            "skip": "do nothing; no Learn or Review fact is written",
            "long_practice": "available only when explicitly chosen and may exceed 15 minutes",
        },
        "source_revisions": {"learning_commit_id": learning["commit_id"],
                             "card_commit_id": cards["commit_id"],
                             "review_commit_id": reviews["commit_id"]},
    }, validation={"learning_record": "passed", "card_record": "passed",
                   "review_record": "passed", "invalid_cards_excluded": invalid_cards,
                   "recommendation_persisted": False})


def run_suggestions(request: dict[str, Any]) -> dict[str, Any]:
    from .workspace import discover_workspace
    config = discover_workspace(Path(request["workspace"]))
    if request.get("action") != "today":
        return response(status="missing_input", workspace=str(config.config_path),
                        diagnostics=["unsupported suggestions action"])
    return today(config, request.get("on_date"),
                 preferred_question_ids=request.get("preferred_question_ids"),
                 preferred_card_version_ids=request.get("preferred_card_version_ids"))


run_suggestions.__capability_contract__ = {
    "input_type": "suggestions-request-v1", "output_type": "command-response-v1"
}
