import json
import os
import subprocess
import sys
import importlib.util
import shutil
from pathlib import Path

import pytest


def write_workspace(root: Path) -> Path:
    root.mkdir(parents=True)
    config = root / "workspace.toml"
    config.write_text('''schema_version = 2
workspace_id = "11111111-1111-4111-8111-111111111111"
[paths]
project = "project"
results = "results"
sources = "sources"
derived = "derived"
local = "local"
''', encoding="utf-8")
    return config


def cli(*args: object, env: dict[str, str] | None = None) -> tuple[int, dict]:
    completed = subprocess.run(
        [sys.executable, "-m", "video_extract.cli", *map(str, args)],
        cwd=Path(__file__).parents[1], env={**os.environ, **(env or {})},
        capture_output=True, text=True,
    )
    assert completed.stdout, completed.stderr
    return completed.returncode, json.loads(completed.stdout)


def prepare_request(path: Path) -> Path:
    request = {
        "schema_version": 1,
        "practice_id": "practice-11111111-1111-4111-8111-111111111111",
        "question_id": "question-22222222-2222-4222-8222-222222222222",
        "title": "丢弃过期识别结果",
        "scope": "只实现按时间戳过滤一个模拟识别结果",
        "effort_minutes": 20,
        "completion_criteria": ["正常结果保留", "过期结果丢弃", "恰好等于期限时保留"],
        "simulation_scope": "只验证模拟时钟和识别结果，不代表机器人硬件时序或可靠性",
        "files": {
            "example": {"example.py": "def is_fresh(age, limit):\n    return age <= limit\n"},
            "task": {"solution.py": "def keep(result, now, max_age):\n    # TODO: user writes this\n    raise NotImplementedError\n"},
            "tests": {"test_solution.py": "# fixture tests: normal, expired, boundary\n"},
            "reference": {"solution.py": "def keep(result, now, max_age):\n    return now - result['at'] <= max_age\n"},
        },
        "test_command": ["python", "-m", "pytest", "tests/test_solution.py"],
    }
    path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    return path


def test_prepare_separates_one_small_simulated_mechanism_without_touching_learning(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    request = prepare_request(tmp_path / "practice.json")

    code, prepared = cli("practice", "prepare", "--request", request,
                         "--workspace", config, "--json")

    assert code == 0
    assert prepared["status"] == "completed"
    practice = prepared["result"]["practice"]
    assert practice["effort_minutes"] == 20
    assert practice["simulation_scope"].startswith("只验证模拟时钟")
    assert practice["test_command"] == ["python", "-m", "pytest", "tests/test_solution.py"]
    root = Path(prepared["result"]["workspace"])
    assert (root / "example/example.py").is_file()
    assert "TODO" in (root / "task/solution.py").read_text()
    assert (root / "tests/test_solution.py").is_file()
    assert (root / "reference/solution.py").is_file()
    assert not (config.parent / "results/learning/current.json").exists()


def test_prepare_replay_never_overwrites_user_code_and_rejects_unsafe_paths(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    request = prepare_request(tmp_path / "practice.json")
    _, prepared = cli("practice", "prepare", "--request", request, "--workspace", config, "--json")
    task = Path(prepared["result"]["workspace"]) / "task/solution.py"
    task.write_text("# my work\n", encoding="utf-8")

    code, replayed = cli("practice", "prepare", "--request", request,
                         "--workspace", config, "--json")
    assert code == 0
    assert replayed["result"]["reused"] is True
    assert task.read_text() == "# my work\n"

    value = json.loads(request.read_text()); value["practice_id"] = "practice-33333333-3333-4333-8333-333333333333"
    value["files"]["task"] = {"../escape.py": "bad"}
    request.write_text(json.dumps(value), encoding="utf-8")
    code, rejected = cli("practice", "prepare", "--request", request,
                         "--workspace", config, "--json")
    assert code != 0
    assert rejected["validation"]["request"] == "failed"
    assert not (config.parent / "results/practices/workspaces/escape.py").exists()


def test_checkpoint_captures_stable_user_code_and_preserves_revision_conflicts(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    request = prepare_request(tmp_path / "practice.json")
    _, prepared = cli("practice", "prepare", "--request", request, "--workspace", config, "--json")
    practice_id = prepared["result"]["practice"]["practice_id"]
    task = Path(prepared["result"]["workspace"]) / "task/solution.py"
    task.write_text("def keep(result, now, max_age):\n    return now - result['at'] <= max_age\n")

    code, checkpoint = cli("practice", "checkpoint", "--practice-id", practice_id,
                           "--expected-revision", 1, "--workspace", config, "--json")
    assert code == 0
    assert checkpoint["result"]["checkpoint"]["files"][0]["path"] == "solution.py"
    assert checkpoint["result"]["checkpoint"]["object_sha256"]

    attempt = tmp_path / "attempt.json"
    attempt.write_text(json.dumps({"event_id": "event-1", "kind": "attempt", "summary": "先写了 < 而非 <="}), encoding="utf-8")
    code, conflict = cli("practice", "record", "--practice-id", practice_id,
                         "--request", attempt, "--expected-revision", 1,
                         "--workspace", config, "--json")
    assert code == 3
    assert conflict["status"] == "awaiting_user"
    assert Path(conflict["result"]["candidate"]).is_file()
    assert task.read_text().startswith("def keep")


def test_record_keeps_hint_attempt_and_test_facts_without_executing_or_changing_learning(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    request = prepare_request(tmp_path / "practice.json")
    _, prepared = cli("practice", "prepare", "--request", request, "--workspace", config, "--json")
    practice_id = prepared["result"]["practice"]["practice_id"]
    marker = tmp_path / "must-not-run"
    _, checked = cli("practice", "checkpoint", "--practice-id", practice_id,
                     "--expected-revision", 1, "--workspace", config, "--json")
    revision = checked["result"]["revision"]
    checkpoint = checked["result"]["checkpoint"]
    events = [
        {"event_id": "hint-1", "kind": "hint", "level": 1, "summary": "先比较时间差"},
        {"event_id": "attempt-1", "kind": "attempt", "summary": "修正边界条件"},
        {"event_id": "test-1", "kind": "test", "command": ["touch", str(marker)],
         "cases": {"normal": "passed", "expired": "passed", "boundary": "passed"},
         "exit_code": 0, "observed_at": "2026-09-17T02:00:00+00:00",
         "verification": "tool_observed", "checkpoint_id": checkpoint["checkpoint_id"],
         "code_object_sha256": checkpoint["object_sha256"]},
        {"event_id": "outcome-1", "kind": "outcome", "completion": "with_hint",
         "next_step": "尝试改变过期阈值"},
    ]
    for event in events:
        event_path = tmp_path / f'{event["event_id"]}.json'
        event_path.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
        code, recorded = cli("practice", "record", "--practice-id", practice_id,
                             "--request", event_path, "--expected-revision", revision,
                             "--workspace", config, "--json")
        assert code == 0
        revision = recorded["result"]["revision"]
    assert not marker.exists()
    assert recorded["result"]["practice"]["completion"] == "with_hint"
    assert recorded["result"]["practice"]["reported_outcome"] == "with_hint"
    assert len(recorded["result"]["practice"]["events"]) == 4
    assert not (config.parent / "results/learning/current.json").exists()


def test_checkpoint_reports_continuously_changing_code_without_false_snapshot(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    request = prepare_request(tmp_path / "practice.json")
    _, prepared = cli("practice", "prepare", "--request", request, "--workspace", config, "--json")
    practice_id = prepared["result"]["practice"]["practice_id"]

    code, result = cli("practice", "checkpoint", "--practice-id", practice_id,
                       "--expected-revision", 1, "--workspace", config, "--json",
                       env={"VIDEO_EXTRACT_PRACTICE_TEST_FAULT": "unstable_read"})
    assert code != 0
    assert result["status"] == "recoverable_failure"
    assert result["validation"]["stable_code"] == "failed"
    assert result["result"]["revision"] == 1


def test_stale_recognition_fixture_covers_normal_expired_and_boundary() -> None:
    solution = Path(__file__).parent / "fixtures/practice/stale_recognition/reference/solution.py"
    spec = importlib.util.spec_from_file_location("fixture_solution", solution)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    assert module.keep({"observed_at": 98.0}, 100.0, 5.0) is True
    assert module.keep({"observed_at": 94.0}, 100.0, 5.0) is False
    assert module.keep({"observed_at": 95.0}, 100.0, 5.0) is True


def test_backup_entries_pin_checkpointed_code_and_exclude_editable_workspace(tmp_path: Path) -> None:
    from video_extract.practice import backup_entries
    from video_extract.workspace import WorkspaceConfig
    config_path = write_workspace(tmp_path / "workspace")
    request = prepare_request(tmp_path / "practice.json")
    _, prepared = cli("practice", "prepare", "--request", request, "--workspace", config_path, "--json")
    pid = prepared["result"]["practice"]["practice_id"]
    _, checked = cli("practice", "checkpoint", "--practice-id", pid, "--expected-revision", 1,
                     "--workspace", config_path, "--json")

    entries = backup_entries(WorkspaceConfig.load(config_path))["entries"]
    assert any("/objects/" in item for item in entries)
    assert all("/workspaces/" not in item for item in entries)
    assert checked["result"]["checkpoint"]["object_sha256"] in " ".join(entries)

    restored = tmp_path / "restored"
    restored_config = write_workspace(restored)
    source_root = config_path.parent / "results"
    target_root = restored / "results"
    for entry in entries:
        source = Path(entry)
        relative = source.relative_to(source_root)
        destination = target_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    restored_entries = backup_entries(WorkspaceConfig.load(restored_config))
    assert restored_entries["commit_id"] == checked["result"]["commit_id"]


@pytest.mark.parametrize("fault", ["late_file", "remove_file", "type_change", "symlink_swap", "late_after_second"])
def test_checkpoint_rejects_file_set_and_type_races(tmp_path: Path, fault: str) -> None:
    config = write_workspace(tmp_path / "workspace")
    request = prepare_request(tmp_path / "practice.json")
    _, prepared = cli("practice", "prepare", "--request", request, "--workspace", config, "--json")
    pid = prepared["result"]["practice"]["practice_id"]

    code, result = cli("practice", "checkpoint", "--practice-id", pid,
                       "--expected-revision", 1, "--workspace", config, "--json",
                       env={"VIDEO_EXTRACT_PRACTICE_TEST_FAULT": fault})

    assert code == 1
    assert result["status"] == "recoverable_failure"
    assert result["validation"]["stable_code"] == "failed"
    assert result["result"]["revision"] == 1


def test_outcome_without_checkpoint_attempt_and_observed_tests_is_not_authoritative(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    request = prepare_request(tmp_path / "practice.json")
    _, prepared = cli("practice", "prepare", "--request", request, "--workspace", config, "--json")
    pid = prepared["result"]["practice"]["practice_id"]
    outcome = tmp_path / "outcome.json"
    outcome.write_text(json.dumps({"event_id": "outcome-zero", "kind": "outcome",
                                   "completion": "independent", "next_step": "done"}))

    code, result = cli("practice", "record", "--practice-id", pid, "--request", outcome,
                       "--expected-revision", 1, "--workspace", config, "--json")

    assert code == 0
    assert result["result"]["practice"]["completion"] == "in_progress"
    assert result["result"]["practice"]["reported_outcome"] == "independent"


def test_hint_prevents_independent_completion_even_when_outcome_claims_it(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    request = prepare_request(tmp_path / "practice.json")
    _, prepared = cli("practice", "prepare", "--request", request, "--workspace", config, "--json")
    pid = prepared["result"]["practice"]["practice_id"]
    _, checked = cli("practice", "checkpoint", "--practice-id", pid, "--expected-revision", 1,
                     "--workspace", config, "--json")
    revision = checked["result"]["revision"]
    events = [
        {"event_id": "hint-derived", "kind": "hint", "level": 1, "summary": "compare age"},
        {"event_id": "attempt-derived", "kind": "attempt", "summary": "fixed boundary"},
        {"event_id": "test-derived", "kind": "test", "command": ["pytest"],
         "cases": {"normal": "passed", "expired": "passed", "boundary": "passed"},
         "exit_code": 0, "observed_at": "2026-09-17T02:00:00+00:00", "verification": "tool_observed",
         "checkpoint_id": checked["result"]["checkpoint"]["checkpoint_id"],
         "code_object_sha256": checked["result"]["checkpoint"]["object_sha256"]},
        {"event_id": "outcome-derived", "kind": "outcome", "completion": "independent", "next_step": "vary threshold"},
    ]
    for event in events:
        path = tmp_path / f'{event["event_id"]}.json'; path.write_text(json.dumps(event))
        _, result = cli("practice", "record", "--practice-id", pid, "--request", path,
                        "--expected-revision", revision, "--workspace", config, "--json")
        revision = result["result"]["revision"]
    assert result["result"]["practice"]["completion"] == "with_hint"
    assert result["result"]["practice"]["reported_outcome"] == "independent"


def test_malformed_event_is_rejected_without_advancing_revision(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    request = prepare_request(tmp_path / "practice.json")
    _, prepared = cli("practice", "prepare", "--request", request, "--workspace", config, "--json")
    pid = prepared["result"]["practice"]["practice_id"]
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps({"event_id": "bad", "kind": "hint", "level": 1,
                                     "summary": "hint", "unexpected": "whole chat"}))
    code, rejected = cli("practice", "record", "--practice-id", pid, "--request", malformed,
                         "--expected-revision", 1, "--workspace", config, "--json")
    assert code == 1
    assert rejected["validation"]["request"] == "failed"
    outcome = tmp_path / "valid.json"
    outcome.write_text(json.dumps({"event_id": "valid", "kind": "outcome",
                                   "completion": "incomplete", "next_step": "retry"}))
    _, valid = cli("practice", "record", "--practice-id", pid, "--request", outcome,
                   "--expected-revision", 1, "--workspace", config, "--json")
    assert valid["result"]["revision"] == 2


def test_reported_but_unexecuted_test_fact_cannot_complete_practice(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    request = prepare_request(tmp_path / "practice.json")
    _, prepared = cli("practice", "prepare", "--request", request, "--workspace", config, "--json")
    pid = prepared["result"]["practice"]["practice_id"]
    _, checked = cli("practice", "checkpoint", "--practice-id", pid, "--expected-revision", 1,
                     "--workspace", config, "--json")
    revision = checked["result"]["revision"]
    for event in (
        {"event_id": "attempt-reported", "kind": "attempt", "summary": "implemented boundary"},
        {"event_id": "test-reported", "kind": "test", "command": ["pytest"],
         "cases": {"normal": "passed", "expired": "passed", "boundary": "passed"},
         "exit_code": 0, "observed_at": "2026-09-17T02:00:00+00:00",
         "verification": "reported_not_executed",
         "checkpoint_id": checked["result"]["checkpoint"]["checkpoint_id"],
         "code_object_sha256": checked["result"]["checkpoint"]["object_sha256"]},
    ):
        path = tmp_path / f'{event["event_id"]}.json'; path.write_text(json.dumps(event))
        _, result = cli("practice", "record", "--practice-id", pid, "--request", path,
                        "--expected-revision", revision, "--workspace", config, "--json")
        revision = result["result"]["revision"]
    assert result["result"]["practice"]["completion"] == "in_progress"


def test_new_checkpoint_resets_completion_until_that_code_is_tested(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    request = prepare_request(tmp_path / "practice.json")
    _, prepared = cli("practice", "prepare", "--request", request, "--workspace", config, "--json")
    pid = prepared["result"]["practice"]["practice_id"]
    _, first = cli("practice", "checkpoint", "--practice-id", pid, "--expected-revision", 1,
                   "--workspace", config, "--json")
    revision = first["result"]["revision"]; checkpoint = first["result"]["checkpoint"]
    for event in (
        {"event_id": "attempt-first", "kind": "attempt", "summary": "implemented"},
        {"event_id": "test-first", "kind": "test", "command": ["pytest"],
         "cases": {"normal": "passed", "expired": "passed", "boundary": "passed"}, "exit_code": 0,
         "observed_at": "2026-09-17T02:00:00+00:00", "verification": "tool_observed",
         "checkpoint_id": checkpoint["checkpoint_id"], "code_object_sha256": checkpoint["object_sha256"]},
    ):
        path=tmp_path/f'{event["event_id"]}.json';path.write_text(json.dumps(event))
        _, result=cli("practice","record","--practice-id",pid,"--request",path,"--expected-revision",revision,"--workspace",config,"--json")
        revision=result["result"]["revision"]
    assert result["result"]["practice"]["completion"] == "independent"
    _, second = cli("practice", "checkpoint", "--practice-id", pid, "--expected-revision", revision,
                    "--workspace", config, "--json")
    assert second["result"]["practice"]["completion"] == "in_progress"


def test_latest_failure_for_current_checkpoint_overrides_earlier_pass(tmp_path: Path) -> None:
    config=write_workspace(tmp_path/"workspace");request=prepare_request(tmp_path/"practice.json")
    _,prepared=cli("practice","prepare","--request",request,"--workspace",config,"--json");pid=prepared["result"]["practice"]["practice_id"]
    _,checked=cli("practice","checkpoint","--practice-id",pid,"--expected-revision",1,"--workspace",config,"--json")
    revision=checked["result"]["revision"]; checkpoint=checked["result"]["checkpoint"]
    events=[{"event_id":"attempt-latest","kind":"attempt","summary":"implemented"}]
    for name,state,exit_code in (("pass","passed",0),("fail","failed",1)):
        events.append({"event_id":f"test-{name}","kind":"test","command":["pytest"],
            "cases":{"normal":state,"expired":state,"boundary":state},"exit_code":exit_code,
            "observed_at":"2026-09-17T02:00:00+00:00","verification":"tool_observed",
            "checkpoint_id":checkpoint["checkpoint_id"],"code_object_sha256":checkpoint["object_sha256"]})
    for event in events:
        path=tmp_path/f'{event["event_id"]}.json';path.write_text(json.dumps(event))
        _,result=cli("practice","record","--practice-id",pid,"--request",path,"--expected-revision",revision,"--workspace",config,"--json")
        revision=result["result"]["revision"]
    assert result["result"]["practice"]["completion"] == "in_progress"


def test_test_event_rejects_checkpoint_code_identity_mismatch(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    request = prepare_request(tmp_path / "practice.json")
    _, prepared = cli("practice", "prepare", "--request", request, "--workspace", config, "--json")
    pid = prepared["result"]["practice"]["practice_id"]
    _, checked = cli("practice", "checkpoint", "--practice-id", pid, "--expected-revision", 1,
                     "--workspace", config, "--json")
    checkpoint = checked["result"]["checkpoint"]
    event = tmp_path / "mismatched-test.json"
    event.write_text(json.dumps({
        "event_id": "test-mismatch", "kind": "test", "command": ["pytest"],
        "cases": {"normal": "passed", "expired": "passed", "boundary": "passed"},
        "exit_code": 0, "observed_at": "2026-09-17T02:00:00+00:00",
        "verification": "tool_observed", "checkpoint_id": checkpoint["checkpoint_id"],
        "code_object_sha256": "0" * 64,
    }))

    code, rejected = cli("practice", "record", "--practice-id", pid, "--request", event,
                         "--expected-revision", checked["result"]["revision"],
                         "--workspace", config, "--json")

    assert code == 1
    assert rejected["validation"]["request"] == "failed"
