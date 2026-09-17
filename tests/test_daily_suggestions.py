"""T18 deterministic daily suggestions and Codex choice boundaries."""

import hashlib
import json
from pathlib import Path

from test_card_workflow import _propose, _select
from test_review_workflow import cli, explained_question
from video_extract.learning import record_feedback


def _store_hashes(config) -> dict[str, str | None]:
    result = {}
    for name in ("learning", "cards", "review"):
        path = config.results / name / "current.json"
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return result


def test_today_prioritizes_due_card_and_is_read_only(tmp_path: Path) -> None:
    config, _, question_id = explained_question(tmp_path)
    card = _select(config, tmp_path, _propose(config, question_id))
    before = _store_hashes(config)

    code, result = cli("suggestions", "today", "--on-date", "2026-09-18",
                       "--workspace", config.config_path, "--json")

    assert code == 0
    assert _store_hashes(config) == before
    assert len(result["result"]["suggestions"]) == 1
    suggestion = result["result"]["suggestions"][0]
    assert suggestion["kind"] == "card_review"
    assert suggestion["object_version"] == card["active_version_id"]
    assert suggestion["estimated_minutes"] == 5
    assert result["result"]["total_estimated_minutes"] == 5
    assert result["result"]["maximum_total_minutes"] == 15
    assert result["validation"]["recommendation_persisted"] is False
    assert "long_practice" in result["result"]["selection"]


def test_recalculation_removes_completed_review_and_parked_question(tmp_path: Path) -> None:
    config, _, question_id = explained_question(tmp_path)
    card = _select(config, tmp_path, _propose(config, question_id))
    version_id = card["active_version_id"]
    code, prepared = cli("review", "prepare", "--card-version-id", version_id,
                         "--workspace", config.config_path, "--json")
    assert code == 3
    assert cli("review", "record", "--preparation-id", prepared["result"]["preparation_id"],
               "--event-id", "daily-review-complete", "--answer", "拒绝过期结果",
               "--model-evaluation", "recalled", "--review-date", "2026-09-18",
               "--workspace", config.config_path, "--json")[0] == 0
    record_feedback(config, question_id, "parked", "暂时不继续这个问题")

    code, result = cli("suggestions", "today", "--on-date", "2026-09-18",
                       "--workspace", config.config_path, "--json")

    assert code == 0
    assert result["result"]["suggestions"] == []
    assert result["result"]["empty_reason"] == "no_eligible_started_learning_or_due_cards"


def test_confused_started_question_is_suggested_but_understood_is_not(tmp_path: Path) -> None:
    config, _, question_id = explained_question(tmp_path)
    record_feedback(config, question_id, "confused", "我还不能解释矩阵列的因果")

    code, result = cli("suggestions", "today", "--on-date", "2026-09-18",
                       "--workspace", config.config_path, "--json")
    assert code == 0
    assert result["result"]["suggestions"][0]["kind"] == "question_review"
    assert result["result"]["suggestions"][0]["question_id"] == question_id
    assert result["result"]["suggestions"][0]["estimated_minutes"] == 10

    record_feedback(config, question_id, "understood", "现在可以独立解释")
    assert cli("suggestions", "today", "--on-date", "2026-09-18",
               "--workspace", config.config_path, "--json")[1]["result"]["suggestions"] == []

    code, preferred = cli("suggestions", "today", "--on-date", "2026-09-18",
                          "--prefer-question-id", question_id,
                          "--workspace", config.config_path, "--json")
    assert code == 0
    assert preferred["result"]["suggestions"][0]["question_id"] == question_id
    assert "明确选择" in preferred["result"]["suggestions"][0]["reason"]


def test_completed_question_review_is_not_suggested_again_that_day(tmp_path: Path) -> None:
    config, _, question_id = explained_question(tmp_path)
    code, prepared = cli("review", "prepare", "--question-id", question_id,
                         "--workspace", config.config_path, "--json")
    assert code == 3
    assert cli("review", "record", "--preparation-id", prepared["result"]["preparation_id"],
               "--event-id", "question-reviewed-today", "--answer", "矩阵列是基向量的像",
               "--model-evaluation", "recalled", "--review-date", "2026-09-18",
               "--workspace", config.config_path, "--json")[0] == 0

    code, result = cli("suggestions", "today", "--on-date", "2026-09-18",
                       "--workspace", config.config_path, "--json")
    assert code == 0
    assert result["result"]["suggestions"] == []


def test_daily_group_has_at_most_three_items_and_fifteen_minutes(tmp_path: Path) -> None:
    config, _, question_id = explained_question(tmp_path)
    for index in range(4):
        candidate = _propose(config, question_id, target=f"强化目标 {index}",
                             conditions=f"适用条件 {index}")
        _select(config, tmp_path, candidate)

    code, result = cli("suggestions", "today", "--on-date", "2026-09-18",
                       "--workspace", config.config_path, "--json")
    assert code == 0
    assert len(result["result"]["suggestions"]) == 3
    assert result["result"]["total_estimated_minutes"] == 15


def test_corrupt_authoritative_store_is_failed_not_empty(tmp_path: Path) -> None:
    config, _, _ = explained_question(tmp_path)
    pointer = config.results / "learning/current.json"
    value = json.loads(pointer.read_text(encoding="utf-8"))
    value["manifest_sha256"] = "0" * 64
    pointer.write_text(json.dumps(value), encoding="utf-8")

    code, result = cli("suggestions", "today", "--on-date", "2026-09-18",
                       "--workspace", config.config_path, "--json")

    assert code == 1
    assert result["status"] == "failed"
    assert result["result"] is None
    assert "corrupt" in result["diagnostics"][0]


def test_suggestions_capability_uses_the_same_read_only_contract(tmp_path: Path) -> None:
    config, _, question_id = explained_question(tmp_path)
    request = tmp_path / "suggestions.json"
    request.write_text(json.dumps({"contract_version": 1, "workspace": str(config.config_path),
                                   "action": "today", "on_date": "2026-09-18"}), encoding="utf-8")
    code, result = cli("capability", "run", "learning.suggestions", "--request", request, "--json")
    assert code == 0
    assert result["result"]["suggestions"][0]["question_id"] == question_id
