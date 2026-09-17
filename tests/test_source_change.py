import json
import os
import subprocess
import sys
from pathlib import Path


def cli(*args: object) -> tuple[int, dict]:
    completed = subprocess.run(
        [sys.executable, "-m", "video_extract.cli", *map(str, args)],
        cwd=Path(__file__).parents[1], env=os.environ.copy(), capture_output=True, text=True,
    )
    assert completed.stdout, completed.stderr
    return completed.returncode, json.loads(completed.stdout)


def workspace(root: Path) -> Path:
    root.mkdir()
    config = root / "workspace.toml"
    config.write_text('''schema_version = 2
workspace_id = "11111111-1111-4111-8111-111111111111"
[paths]
project = "project"
results = "results"
sources = "sources"
derived = "derived"
local = "local"
''')
    return config


def register(path: Path, config: Path) -> dict:
    code, value = cli("source", "register", path, "--workspace", config, "--json")
    assert code == 0
    return value["result"]


def test_source_change_marks_only_its_pinned_consumers_for_review(tmp_path: Path) -> None:
    config = workspace(tmp_path / "workspace")
    course_path = tmp_path / "course.md"; course_path.write_text("# Rule\nold fact\n")
    supplement_path = tmp_path / "supplement.md"; supplement_path.write_text("# Concept\nstable fact\n")
    course = register(course_path, config); supplement = register(supplement_path, config)
    _, course_module = cli("learning", "module", "create", "--goal", "course", "--scope", "rule",
                           "--source-id", course["source_id"], "--source-version", course["source_version"],
                           "--workspace", config, "--json")
    _, supplement_module = cli("learning", "module", "create", "--goal", "concept", "--scope", "stable",
                               "--source-id", supplement["source_id"], "--source-version", supplement["source_version"],
                               "--source-role", "supplemental_source", "--workspace", config, "--json")

    course_path.write_text("# Rule\ncorrected fact\n")
    current = register(course_path, config)

    _, old = cli("source", "verify", course["source_id"], "--source-version", course["source_version"],
                 "--workspace", config, "--json")
    assert old["result"]["version_state"] == "historical"
    assert old["result"]["current_version"] == current["source_version"]
    assert old["result"]["change_check"] == "needs_review"

    code, stale_note = cli("notes", "prepare", course["source_id"],
                           "--source-version", course["source_version"], "--workspace", config, "--json")
    assert code == 3
    assert stale_note["status"] == "awaiting_user"
    assert stale_note["result"]["source_check"] == "needs_review"

    _, stale_module = cli("learning", "module", "show", course_module["result"]["module"]["module_id"],
                          "--workspace", config, "--json")
    _, stable_module = cli("learning", "module", "show", supplement_module["result"]["module"]["module_id"],
                           "--workspace", config, "--json")
    assert stale_module["result"]["source_checks"][0]["status"] == "needs_review"
    assert stable_module["result"]["source_checks"][0]["status"] == "current"


def test_changed_key_evidence_preserves_old_explanation_location_but_pauses_that_question(tmp_path: Path) -> None:
    config = workspace(tmp_path / "workspace")
    source_path = tmp_path / "course.md"; source_path.write_text("# Basis\ncolumns are basis images\n")
    source = register(source_path, config)
    _, module = cli("learning", "module", "create", "--goal", "linear", "--scope", "basis",
                    "--source-id", source["source_id"], "--source-version", source["source_version"],
                    "--workspace", config, "--json")
    _, rooted = cli("learning", "thread", "create", "--module-id", module["result"]["module"]["module_id"],
                    "--root-question", "why columns?", "--workspace", config, "--json")
    question_id = rooted["result"]["question"]["question_id"]
    _, prepared = cli("explanation", "prepare", "--question-id", question_id,
                      "--profile", "linear_transform", "--workspace", config, "--json")
    draft = tmp_path / "explanation.md"
    draft.write_text(
        "# Linear\n" + prepared["result"]["required_marker"] +
        "\n直觉与因果机制：矩阵列是基向量的像。例子：[[2,0],[0,3]] 把 (1,2) 变为 (2,6)。"
        "条件边界：平移不是线性变换，要用仿射或齐次坐标。\n")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps([{"source_id": source["source_id"], "source_version": source["source_version"],
                                     "locator": {"kind": "heading", "value": "Basis"},
                                     "claim_type": "course_fact", "claim": "columns are basis images"}]))
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"intuition": True, "causality": True, "mechanism": True,
                                  "worked_example": True, "conditions": True, "source_alignment": True,
                                  "basis_coordinate_reasoning": True, "affine_boundary": True}))
    code, committed = cli("explanation", "commit", "--question-id", question_id, "--draft", draft,
                          "--evidence", evidence, "--teaching-review", review, "--profile", "linear_transform",
                          "--preparation-id", prepared["result"]["preparation_id"], "--workspace", config, "--json")
    assert code == 0
    old_path = committed["result"]["explanation"]["document_path"]

    source_path.write_text("# Basis\ncolumns include an affine offset\n")
    register(source_path, config)

    code, located = cli("learning", "locate", question_id, "--workspace", config, "--json")
    assert code == 0
    assert located["result"]["explanation_state"] == "needs_review"
    assert located["result"]["locations"][0]["document_path"] == old_path
    assert Path(old_path).read_text() == draft.read_text()
    code, blocked = cli("explanation", "prepare", "--question-id", question_id,
                        "--profile", "linear_transform", "--workspace", config, "--json")
    assert code == 3
    assert blocked["status"] == "awaiting_user"
    assert blocked["result"]["explanation_state"] == "needs_review"
    assert blocked["result"]["locations"][0]["document_path"] == old_path


def test_code_version_change_reports_only_changed_inventory_entries(tmp_path: Path) -> None:
    config = workspace(tmp_path / "workspace")
    code_root = tmp_path / "code"; code_root.mkdir()
    (code_root / "changed.py").write_text("value = 1\n")
    (code_root / "stable.py").write_text("stable = True\n")
    old = register(code_root, config)
    (code_root / "changed.py").write_text("value = 2\n")
    current = register(code_root, config)

    _, checked = cli("source", "verify", old["source_id"], "--source-version", old["source_version"],
                     "--workspace", config, "--json")

    assert checked["result"]["current_version"] == current["source_version"]
    assert checked["result"]["change_summary"] == {
        "added": [], "modified": ["changed.py"], "removed": []}
