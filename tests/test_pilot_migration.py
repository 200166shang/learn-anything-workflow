import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]


def cli(*args: object, env: dict[str, str] | None = None) -> tuple[int, dict]:
    completed = subprocess.run(
        [sys.executable, "-m", "video_extract.cli", *map(str, args)],
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
    )
    assert completed.stdout, completed.stderr
    return completed.returncode, json.loads(completed.stdout)


def workspace(root: Path) -> Path:
    root.mkdir()
    config = root / "workspace.toml"
    config.write_text(
        '''schema_version = 2
workspace_id = "99999999-9999-4999-8999-999999999999"
[paths]
project = "project"
results = "results"
sources = "sources"
derived = "derived"
local = "local"
''',
        encoding="utf-8",
    )
    return config


def legacy_pilot(root: Path) -> Path:
    package = root / "course"
    (package / "notes/images").mkdir(parents=True)
    (package / "notes/lesson.md").write_text(
        "# 第一课\n\n证据见 ![坐标图](images/frame.png)\n", encoding="utf-8"
    )
    (package / "notes/images/frame.png").write_bytes(b"legacy-image")
    (package / "manifest.json").write_text(json.dumps({
        "schema_version": 4,
        "platform": "local",
        "identity": "legacy-course-1",
        "title": "旧课程",
        "source_version": "legacy-rev-7",
        "artifacts": {"notes": "notes/lesson.md", "image": "notes/images/frame.png"},
    }), encoding="utf-8")
    (root / "thread.json").write_text(json.dumps({
        "schema_version": 1,
        "module": {"goal": "理解坐标变换", "scope": "旧课程第一课"},
        "thread": {"id": "thread-old", "root_question_id": "q001", "current_question_id": "q002",
                   "return_route": [{"question_id": "q001", "entered_at": "2026-09-16T00:00:00+00:00"}],
                   "entry_history": [{"id": "entered-q002", "from_question_id": "q001",
                                      "to_question_id": "q002", "original_text": "继续追问基向量",
                                      "relation": "deepens", "created_at": "2026-09-16T00:01:00+00:00"}]},
        "questions": [
            {"id": "q001", "text": "为什么要变换坐标？",
             "locator": {"path": "notes/lesson.md", "heading": "第一课"}},
            {"id": "q002", "text": "基向量如何参与？", "parent_id": "q001", "relation": "deepens"},
        ],
    }), encoding="utf-8")
    return root


def authorize(root: Path, batch: str, legacy: Path) -> Path:
    path = root / f"{batch}-authorization.json"
    path.write_text(json.dumps({"batch": batch, "legacy_package": str((legacy / "course").resolve()),
                                "legacy_thread": str((legacy / "thread.json").resolve()),
                                "approved": True}), encoding="utf-8")
    return path


def test_public_migration_stops_cutover_when_source_changed(tmp_path: Path) -> None:
    config = workspace(tmp_path / "workspace")
    legacy = legacy_pilot(tmp_path / "legacy")
    batch = "pilot-one"

    code, planned = cli("migration", "plan", "--batch", batch, "--legacy-package", legacy / "course",
                        "--legacy-thread", legacy / "thread.json", "--workspace", config, "--json")
    assert code == 0
    assert planned["result"]["inventory"]["counts"] == {
        "notes": 1, "images": 1, "questions": 2, "relationships": 1,
        "feedbacks": 0, "operation_receipts": 0, "return_route": 1,
        "entry_history": 1, "locators": 1,
    }
    assert planned["result"]["inventory"]["image_references"] == [{
        "note": "notes/lesson.md", "target": "images/frame.png",
        "external": False, "present": True,
    }]
    assert planned["result"]["inventory"]["missing_facts"] == ["feedbacks", "operation_receipts"]
    assert planned["result"]["engineering_basis"] == {
        "git_commit": None, "working_tree_dirty": None,
    }

    assert cli("migration", "convert", "--batch", batch, "--workspace", config, "--json")[0] == 0
    verify_code, verified = cli("migration", "verify", "--batch", batch,
                                "--workspace", config, "--json")
    assert verify_code == 0
    assert verified["validation"]["content"] == "passed"
    assert verified["result"]["identity_map"]["q001"].startswith("question-")

    (legacy / "course/notes/lesson.md").write_text("# changed after verification\n", encoding="utf-8")
    authorization = authorize(tmp_path, batch, legacy)
    cutover_code, blocked = cli("migration", "cutover", "--batch", batch,
                                "--workspace", config, "--authorization", authorization, "--json")
    assert cutover_code != 0
    assert blocked["status"] == "recoverable_failure"
    assert blocked["validation"]["source_unchanged"] == "failed"
    assert not (config.parent / "results/migration-ownership.json").exists()
    batch_state = json.loads((config.parent / "local/migration-batches" / batch / "batch.json").read_text())
    assert batch_state["acceptance"]["real_pilot"] == "pending_authorization"
    assert batch_state["events"][-1]["operation"] == "migration cutover"
    assert batch_state["events"][-1]["status"] == "failed"


def test_cutover_single_ownership_and_rollback_preserves_increment(tmp_path: Path) -> None:
    config = workspace(tmp_path / "workspace")
    legacy = legacy_pilot(tmp_path / "legacy")
    batch = "pilot-two"
    common = ("--batch", batch, "--workspace", config, "--json")
    assert cli("migration", "plan", "--legacy-package", legacy / "course",
               "--legacy-thread", legacy / "thread.json", *common)[0] == 0
    assert cli("migration", "convert", *common)[0] == 0
    assert cli("migration", "verify", *common)[0] == 0
    authorization = authorize(tmp_path, batch, legacy)
    failed_code, failed = cli("migration", "cutover", *common, "--authorization", authorization,
                              env={"VIDEO_EXTRACT_MIGRATION_TEST_FAULT": "after_legacy_read_only"})
    assert failed_code != 0 and "injected failure" in failed["diagnostics"][0]
    assert not (config.parent / "results/migration-ownership.json").exists()
    code, cutover = cli("migration", "cutover", *common, "--authorization", authorization)
    assert code == 0 and cutover["status"] == "completed"
    retry_code, retried = cli("migration", "cutover", *common, "--authorization", authorization)
    assert retry_code == 0 and retried["result"]["reconciled"] is True

    ownership = json.loads((config.parent / "results/migration-ownership.json").read_text())
    assert ownership["batches"][batch]["owner"] == "new"
    assert ownership["batches"][batch]["legacy_read_only"] is True
    assert (legacy / "course/manifest.json").stat().st_mode & 0o222 == 0
    assert (legacy / "thread.json").stat().st_mode & 0o222 == 0
    migrated = Path(cutover["result"]["migrated_course"])
    assert (migrated / "notes/lesson.md").read_text(encoding="utf-8").startswith("# 第一课")
    assert (migrated / "notes/images/frame.png").read_bytes() == b"legacy-image"
    thread_id = cutover["result"]["learning_commit_id"]
    assert thread_id.startswith("learning-commit-")
    mapped_thread = json.loads(
        (config.parent / "local/migration-batches" / batch / "converted/conversion.json").read_text()
    )["identity_map"]["thread-old"]
    show_code, shown = cli("learning", "thread", "show", mapped_thread,
                           "--workspace", config, "--json")
    assert show_code == 0
    assert shown["result"]["thread"]["current_question_id"] == next(
        item["question_id"] for item in shown["result"]["questions"]
        if item["original_question"] == "基向量如何参与？"
    )
    assert all(state["feedback_history"] == [] for state in shown["result"]["question_states"].values())
    assert len(shown["result"]["thread"]["return_route"]) == 1
    assert len(shown["result"]["thread"]["entry_history"]) == 1
    root_question = next(item["question_id"] for item in shown["result"]["questions"]
                         if item["original_question"] == "为什么要变换坐标？")
    locate_code, located = cli("learning", "locate", root_question,
                               "--workspace", config, "--json")
    assert locate_code == 0
    assert located["result"]["explanation_state"] == "legacy_migrated"
    assert Path(located["result"]["locations"][0]["document_path"]).is_file()

    feedback_code, _ = cli("learning", "feedback", "--question-id", root_question,
                           "--state", "confused", "--text", "切换后仍有疑惑",
                           "--workspace", config, "--json")
    assert feedback_code == 0

    increment = migrated / "new-after-cutover.md"
    increment.write_text("切换后的新成果", encoding="utf-8")
    receipts = config.parent / "results/operation-receipts"
    receipts.mkdir()
    (receipts / "publish.json").write_text('{"status":"confirmed"}', encoding="utf-8")
    failed_rollback_code, _ = cli(
        "migration", "rollback", *common,
        env={"VIDEO_EXTRACT_MIGRATION_TEST_FAULT": "after_rollback_frozen"},
    )
    assert failed_rollback_code != 0
    blocked_during_code, blocked_during = cli(
        "learning", "feedback", "--question-id", root_question,
        "--state", "understood", "--text", "回退冻结后不应写入",
        "--workspace", config, "--json",
    )
    assert blocked_during_code != 0
    assert "owned by the legacy store" in blocked_during["diagnostics"][0]
    code, rolled_back = cli("migration", "rollback", *common)
    assert code == 0
    preserved = Path(rolled_back["result"]["preserved_increment"])
    assert (preserved / "new-after-cutover.md").read_text(encoding="utf-8") == "切换后的新成果"
    assert (preserved / "operation-receipts/publish.json").is_file()
    assert (preserved / "learning/current.json").is_file()
    ownership = json.loads((config.parent / "results/migration-ownership.json").read_text())
    assert ownership["batches"][batch]["owner"] == "legacy"
    assert ownership["batches"][batch]["replay_required"] is True
    assert increment.is_file()
    assert increment.stat().st_mode & 0o222 == 0
    assert (legacy / "course/manifest.json").stat().st_mode & 0o200
    assert (legacy / "thread.json").stat().st_mode & 0o200
    blocked_code, blocked_write = cli("learning", "feedback", "--question-id", root_question,
                                      "--state", "understood", "--text", "不应写入新位置",
                                      "--workspace", config, "--json")
    assert blocked_code != 0
    assert "owned by the legacy store" in blocked_write["diagnostics"][0]
    unrelated_receipt = receipts / "unrelated-after-rollback.json"
    unrelated_receipt.write_text('{"status":"new operation"}', encoding="utf-8")
    assert unrelated_receipt.is_file()
