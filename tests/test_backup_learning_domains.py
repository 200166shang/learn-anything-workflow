"""Backup/restore CLI round trips across independent learning domains."""
import json
import hashlib
from pathlib import Path

import pytest

from test_review_workflow import cli, explained_question
from test_practice_workflow import prepare_request, write_workspace
from test_backup_restore import populated


def test_cli_restores_recall_history_and_user_checkpoint_without_original_results(tmp_path: Path) -> None:
    config, _, question_id = explained_question(tmp_path)
    for index in range(2):
        code, prepared = cli("review", "prepare", "--question-id", question_id,
                             "--workspace", config.config_path, "--json")
        assert code == 3
        code, recorded = cli("review", "record", "--preparation-id", prepared["result"]["preparation_id"],
                             "--event-id", f"recall-{index}", "--answer", "Columns are basis images",
                             "--model-evaluation", "recalled", "--workspace", config.config_path, "--json")
        assert code == 0
    request = prepare_request(tmp_path / "practice.json")
    value = json.loads(request.read_text()); value["question_id"] = question_id
    request.write_text(json.dumps(value))
    code, practice = cli("practice", "prepare", "--request", request, "--workspace", config.config_path, "--json")
    assert code == 0
    code_text = "def keep(result, now, max_age):\n    return now - result['at'] <= max_age\n"
    (Path(practice["result"]["workspace"]) / "task/solution.py").write_text(code_text)
    pid = practice["result"]["practice"]["practice_id"]
    code, checkpoint = cli("practice", "checkpoint", "--practice-id", pid, "--expected-revision", 1,
                           "--workspace", config.config_path, "--json")
    assert code == 0
    backup = tmp_path / "backup"
    code, created = cli("backup", "create", backup, "--workspace", config.config_path, "--json")
    assert code == 0, created
    assert cli("backup", "verify", backup, "--json")[0] == 0
    config.results.rename(tmp_path / "original-results-unavailable")
    restored_config = write_workspace(tmp_path / "restored")
    code, restored = cli("backup", "restore", backup, "--workspace", restored_config, "--json")
    assert code == 0, restored
    code, history = cli("review", "show", "--workspace", restored_config, "--json")
    assert code == 0
    assert [event["event_id"] for event in history["result"]["events"]] == ["recall-0", "recall-1"]
    code, replayed = cli("practice", "prepare", "--request", request, "--workspace", restored_config, "--json")
    assert code == 0, replayed
    assert replayed["result"]["practice"]["checkpoints"] == checkpoint["result"]["practice"]["checkpoints"]
    digest = checkpoint["result"]["checkpoint"]["object_sha256"]
    restored_object = restored_config.parent / "results/practices/objects" / digest[:2] / digest
    assert json.loads(restored_object.read_text()) == {"solution.py": code_text}
    manifest = json.loads((backup / "backup-manifest.json").read_text())
    paths = [entry["path"] for entry in manifest["entries"]]
    pin_path = "payload/" + recorded["result"]["event"]["pin"]["logical_path"]
    assert paths.count(pin_path) == 1


def test_cli_restores_legacy_backup_without_optional_domain_metadata(tmp_path: Path) -> None:
    config, _, _ = populated(tmp_path)
    backup = tmp_path / "legacy-backup"
    assert cli("backup", "create", backup, "--workspace", config.config_path, "--json")[0] == 0
    path = backup / "backup-manifest.json"
    manifest = json.loads(path.read_text())
    for name in ("review", "practice"):
        del manifest["authorities"][name]
    for authority in manifest["authorities"].values():
        authority.pop("entry_paths", None)
    unsigned = {key: value for key, value in manifest.items() if key != "commit_id"}
    manifest["commit_id"] = "backup-commit-" + hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    path.write_text(json.dumps(manifest))
    assert cli("backup", "verify", backup, "--json")[0] == 0
    target = write_workspace(tmp_path / "restored")
    assert cli("backup", "restore", backup, "--workspace", target, "--json")[0] == 0


@pytest.mark.parametrize("store", ["review", "practices"])
def test_cli_refuses_backup_when_published_review_or_practice_pointer_is_missing(tmp_path: Path, store: str) -> None:
    config, _, question_id = explained_question(tmp_path)
    if store == "review":
        assert cli("review", "prepare", "--question-id", question_id,
                   "--workspace", config.config_path, "--json")[0] == 3
    else:
        request = prepare_request(tmp_path / "practice.json")
        assert cli("practice", "prepare", "--request", request,
                   "--workspace", config.config_path, "--json")[0] == 0
    (config.results / store / "current.json").unlink()
    backup = tmp_path / "backup"
    code, rejected = cli("backup", "create", backup, "--workspace", config.config_path, "--json")
    assert code != 0 and rejected["status"] == "recoverable_failure"
    assert not backup.exists()
