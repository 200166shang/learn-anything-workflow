"""Public card lifecycle, versioned recall, and reconstructable scheduling."""

import json
from datetime import date
from pathlib import Path

from test_review_workflow import cli, explained_question, workspace


def _propose(config, question_id: str, *, target: str = "识别结果为何需要时间戳",
             conditions: str = "控制器只接受未过期的识别结果") -> dict:
    code, result = cli(
        "card", "propose", "--question-id", question_id,
        "--memory-target", target, "--conditions", conditions,
        "--prompt", "为什么识别结果需要时间戳？",
        "--answer", "控制器要用时间戳拒绝已经过期的结果。",
        "--reason", "记住数据新鲜度的因果边界",
        "--workspace", config.config_path, "--json",
    )
    assert code == 0, result
    return result["result"]["candidate"]


def _select(config, tmp_path: Path, candidate: dict) -> dict:
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
    code, result = cli("card", "select", "--candidate", path,
                       "--effective-date", "2026-09-17",
                       "--workspace", config.config_path, "--json")
    assert code == 0, result
    return result["result"]["card"]


def test_only_selected_candidates_persist_and_same_target_can_be_reused(tmp_path: Path) -> None:
    config, _, question_id = explained_question(tmp_path)
    candidate = _propose(config, question_id)
    assert not (config.results / "cards/current.json").exists()

    card = _select(config, tmp_path, candidate)
    replayed = _select(config, tmp_path, candidate)
    assert replayed["card_id"] == card["card_id"]
    reused = _propose(config, question_id)
    assert reused["proposal"] == "reuse"
    assert reused["existing_card_id"] == card["card_id"]

    other = _propose(config, question_id, conditions="离线批处理允许任意历史结果")
    assert other["proposal"] == "new"
    code, shown = cli("card", "show", "--card-id", card["card_id"],
                      "--workspace", config.config_path, "--json")
    assert code == 0
    stored = shown["result"]["cards"][card["card_id"]]
    assert stored["question_ids"] == [question_id]
    assert stored["selection_events"][0]["candidate_id"] == candidate["candidate_id"]


def test_wording_revision_keeps_schedule_but_material_revision_starts_next_day(tmp_path: Path) -> None:
    config, _, question_id = explained_question(tmp_path)
    card = _select(config, tmp_path, _propose(config, question_id))
    card_id = card["card_id"]
    code, initial = cli("card", "schedule", "--card-id", card_id, "--on-date", "2026-09-17",
                        "--workspace", config.config_path, "--json")
    assert code == 0 and initial["result"]["due_date"] == "2026-09-18"

    code, wording = cli("card", "revise", "--card-id", card_id, "--change-type", "wording",
                        "--answer", "时间戳让控制器拒绝过期识别结果。", "--effective-date", "2026-09-17",
                        "--workspace", config.config_path, "--json")
    assert code == 0 and wording["result"]["version"]["version_number"] == 1
    assert wording["result"]["schedule"]["due_date"] == "2026-09-18"

    code, material = cli("card", "revise", "--card-id", card_id, "--change-type", "material",
                         "--conditions", "实时控制且时钟已经同步", "--effective-date", "2026-09-20",
                         "--workspace", config.config_path, "--json")
    assert code == 0 and material["result"]["version"]["version_number"] == 2
    assert material["result"]["schedule"]["due_date"] == "2026-09-21"


def test_card_review_sequence_is_idempotent_and_schedule_rebuilds_from_facts(tmp_path: Path) -> None:
    config, _, question_id = explained_question(tmp_path)
    card = _select(config, tmp_path, _propose(config, question_id))
    version_id = card["active_version_id"]
    expected = [
        ("2026-09-18", "recalled", "2026-09-21"),
        ("2026-09-22", "recalled", "2026-09-29"),
        ("2026-09-29", "recalled", "2026-10-13"),
        ("2026-10-13", "recalled", "2026-11-12"),
        ("2026-11-12", "recalled", "2026-12-12"),
        ("2026-12-12", "prompted", "2026-12-13"),
        ("2026-12-13", "recalled", "2026-12-16"),
    ]
    last = None
    for index, (review_date, evaluation, due_date) in enumerate(expected):
        code, prepared = cli("review", "prepare", "--card-version-id", version_id,
                             "--preparation-id", f"review-preparation-card-{index}",
                             "--workspace", config.config_path, "--json")
        assert code == 3 and prepared["result"]["answer_revealed"] is False
        code, recorded = cli("review", "record", "--preparation-id", prepared["result"]["preparation_id"],
                             "--event-id", f"card-review-{index}", "--answer", "因为数据会过期",
                             "--model-evaluation", evaluation, "--review-date", review_date,
                             "--workspace", config.config_path, "--json")
        assert code == 0 and recorded["result"]["next_schedule"]["due_date"] == due_date
        last = recorded

    code, replay = cli("review", "record", "--preparation-id", "review-preparation-card-6",
                       "--event-id", "card-review-6", "--answer", "因为数据会过期",
                       "--model-evaluation", "recalled", "--review-date", "2026-12-13",
                       "--workspace", config.config_path, "--json")
    assert code == 0 and replay["result"]["replayed"] is True

    cache = config.derived / "card-schedule-cache.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(last["result"]["next_schedule"]), encoding="utf-8")
    cache.unlink()
    code, rebuilt = cli("card", "schedule", "--card-version-id", version_id,
                        "--on-date", "2026-12-13", "--workspace", config.config_path, "--json")
    assert code == 0
    assert rebuilt["result"]["due_date"] == "2026-12-16"
    assert rebuilt["result"]["derived_from_event_ids"][-1] == "card-review-6"
    code, historical = cli("card", "schedule", "--card-version-id", version_id,
                           "--on-date", "2026-09-22", "--workspace", config.config_path, "--json")
    assert code == 0
    assert historical["result"]["due_date"] == "2026-09-29"
    assert historical["result"]["derived_from_event_ids"] == ["card-review-0", "card-review-1"]
    code, rebuilt_workspace = cli("workspace", "rebuild", "--dry-run",
                                  "--workspace", config.config_path, "--json")
    assert code == 0
    selected = next(item for item in rebuilt_workspace["card_schedules"]
                    if item["card_version_id"] == version_id)
    assert selected["due_date"] == "2026-12-16"


def test_selected_cards_and_history_survive_result_only_backup(tmp_path: Path) -> None:
    config, _, question_id = explained_question(tmp_path)
    card = _select(config, tmp_path, _propose(config, question_id))
    version_id = card["active_version_id"]
    code, prepared = cli("review", "prepare", "--card-version-id", version_id,
                         "--workspace", config.config_path, "--json")
    assert code == 3
    assert cli("review", "record", "--preparation-id", prepared["result"]["preparation_id"],
               "--event-id", "backup-card-review", "--answer", "拒绝过期输入",
               "--model-evaluation", "recalled", "--review-date", "2026-09-18",
               "--workspace", config.config_path, "--json")[0] == 0
    backup = tmp_path / "backup"
    assert cli("backup", "create", backup, "--workspace", config.config_path, "--json")[0] == 0
    restored = workspace(tmp_path / "restored")
    assert cli("backup", "restore", backup, "--workspace", restored.config_path, "--json")[0] == 0
    code, result = cli("card", "schedule", "--card-version-id", version_id,
                       "--on-date", "2026-09-18",
                       "--workspace", restored.config_path, "--json")
    assert code == 0 and result["result"]["due_date"] == "2026-09-21"
