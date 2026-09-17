import json
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from video_extract.capabilities import CAPABILITIES
from video_extract.learning import (commit_explanation, create_module, create_thread,
                                    prepare_explanation, show_thread)
from video_extract.review import backup_entries, prepare, record, show
from video_extract.source_registry import register
from video_extract.workspace import discover_workspace
from video_extract.workspace import WorkspaceError

import pytest


def cli(*args: object) -> tuple[int, dict]:
    completed = subprocess.run([sys.executable, "-m", "video_extract.cli", *map(str, args)],
                               cwd=Path(__file__).parents[1], env=os.environ.copy(),
                               capture_output=True, text=True)
    assert completed.stdout, completed.stderr
    return completed.returncode, json.loads(completed.stdout)


def workspace(tmp_path: Path):
    root = tmp_path / "workspace"; root.mkdir(parents=True)
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
    return discover_workspace(config)


def explained_question(tmp_path: Path):
    config = workspace(tmp_path)
    material = tmp_path / "source.md"; material.write_text("# Basis\nColumns are basis images.\n", encoding="utf-8")
    source = register(config, material)["result"]
    module = create_module(config, "理解线性变换", "矩阵列", source["source_id"], source["source_version"])["result"]["module"]
    rooted = create_thread(config, module["module_id"], "为什么矩阵列代表基向量的去向？")["result"]
    question_id = rooted["question"]["question_id"]
    prepared = prepare_explanation(config, question_id, "linear_transform")["result"]
    draft = tmp_path / "explanation.md"
    draft.write_text(f'''# 线性变换
<!-- section-id: {prepared["section_id"]} -->
直觉：矩阵描述空间改变。因果：任意向量由基向量线性组合。
机制：两列分别是两个基向量的去向。例子：对角矩阵把 (1,2) 变为 (2,6)。
条件与边界：坐标依赖基；平移不是线性变换而属于仿射变换。
''', encoding="utf-8")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps([{"source_id": source["source_id"], "source_version": source["source_version"],
        "locator": {"kind": "heading", "value": "Basis"}, "claim_type": "course_fact", "claim": "矩阵列是基向量的像"}]), encoding="utf-8")
    teaching = tmp_path / "teaching.json"
    teaching.write_text(json.dumps({"intuition": True, "causality": True, "mechanism": True,
        "worked_example": True, "conditions": True, "source_alignment": True,
        "basis_coordinate_reasoning": True, "affine_boundary": True}), encoding="utf-8")
    committed = commit_explanation(config, question_id, draft, evidence, teaching, "linear_transform",
                                   prepared["preparation_id"])
    assert committed["status"] == "completed"
    return config, rooted["thread"]["thread_id"], question_id


def rewrite_review_snapshot(config, mutate) -> None:
    pointer_path = config.results / "review/current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    snapshot_path = config.results / "review/commits" / f'{pointer["commit_id"]}.json'
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    mutate(snapshot)
    body = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"
    snapshot_path.write_bytes(body)
    pointer["manifest_sha256"] = hashlib.sha256(body).hexdigest()
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")


def test_prepare_persists_recall_boundary_without_revealing_answer(tmp_path: Path) -> None:
    config, _, question_id = explained_question(tmp_path)

    result = prepare(config, question_id, "review-preparation-retry-1")
    replay = prepare(config, question_id, "review-preparation-retry-1")

    assert result["status"] == "awaiting_user"
    assert result["result"]["answer_revealed"] is False
    serialized = json.dumps(result, ensure_ascii=False)
    assert "document_path" not in serialized
    assert "object_sha256" not in serialized
    assert replay["result"]["replayed"] is True
    assert replay["result"]["review_commit_id"] == result["result"]["review_commit_id"]
    current = json.loads((config.results / "review" / "current.json").read_text(encoding="utf-8"))
    assert current["commit_id"] == result["result"]["review_commit_id"]


def test_public_cli_and_capability_use_the_same_review_contract(tmp_path: Path) -> None:
    config, _, question_id = explained_question(tmp_path)
    code, prepared = cli("review", "prepare", "--question-id", question_id,
                         "--workspace", config.config_path, "--json")
    assert code == 3
    token = prepared["result"]["preparation_id"]
    code, recorded = cli("review", "record", "--preparation-id", token, "--event-id", "cli-event-1",
                         "--answer", "因为列是基向量的像", "--answer-summary", "基向量的像",
                         "--model-evaluation", "recalled", "--workspace", config.config_path, "--json")
    assert code == 0
    assert recorded["result"]["event"]["pin"]["question_id"] == question_id

    request = tmp_path / "request.json"
    request.write_text(json.dumps({"contract_version": 1, "workspace": str(config.config_path),
                                   "action": "show", "question_id": question_id}), encoding="utf-8")
    code, history = cli("capability", "run", "learning.review", "--request", request, "--json")
    assert code == 0
    assert history["result"]["events"][0]["event_id"] == "cli-event-1"


def test_review_record_is_independent_idempotent_version_pinned_and_user_correctable(tmp_path: Path) -> None:
    config, thread_id, question_id = explained_question(tmp_path)
    before = show_thread(config, thread_id)["result"]
    prepared = prepare(config, question_id)["result"]

    first = record(config, prepared["preparation_id"], "review-event-1",
                   answer="列就是变换后的基向量，所以线性组合仍成立。",
                   answer_summary="用基向量的像解释矩阵列", hints=["先说明线性组合"],
                   model_evaluation="not_recalled", correction="我的推理等价正确，只是措辞不同",
                   corrected_evaluation="recalled")
    replay = record(config, prepared["preparation_id"], "review-event-1",
                    answer="列就是变换后的基向量，所以线性组合仍成立。",
                    answer_summary="用基向量的像解释矩阵列", hints=["先说明线性组合"],
                    model_evaluation="not_recalled", correction="我的推理等价正确，只是措辞不同",
                    corrected_evaluation="recalled")
    conflicting_replay = record(config, prepared["preparation_id"], "review-event-1",
                                answer="不同的事实", answer_summary=None, hints=[],
                                model_evaluation="recalled")
    consumed = record(config, prepared["preparation_id"], "review-event-2",
                      answer="second", answer_summary=None, hints=[], model_evaluation="recalled")

    assert first["status"] == "completed"
    event = first["result"]["event"]
    assert event["model_evaluation"] == "not_recalled"
    assert event["effective_evaluation"] == "recalled"
    assert event["corrections"][0]["text"].startswith("我的推理")
    assert event["pin"]["explanation_revision"] == 1
    assert event["pin"]["source_refs"][0]["source_version"].startswith("source-version-")
    assert replay["result"]["replayed"] is True
    assert conflicting_replay["status"] == "awaiting_user"
    assert conflicting_replay["validation"]["event_id"] == "conflict"
    assert consumed["status"] == "awaiting_user"
    assert show_thread(config, thread_id)["result"] == before
    assert show(config, question_id)["result"]["events"] == [event]
    assert Path(first["result"]["reveal"]["document_path"]).is_file()

    prepare(config, question_id, "review-preparation-second")
    backup = backup_entries(config)
    assert backup["commit_id"] == show(config)["result"]["commit_id"]
    assert all(Path(path).is_file() for path in backup["entries"])
    object_entries = [path for path in backup["entries"] if "/learning/objects/" in path]
    assert object_entries == [first["result"]["reveal"]["document_path"]]
    assert CAPABILITIES["learning.review"].input_type == "review-request-v1"


def test_skipped_review_is_not_scored_and_missing_explanation_creates_no_history(tmp_path: Path) -> None:
    config = workspace(tmp_path)
    material = tmp_path / "source.md"; material.write_text("source", encoding="utf-8")
    source = register(config, material)["result"]
    module = create_module(config, "goal", "scope", source["source_id"], source["source_version"])["result"]["module"]
    question = create_thread(config, module["module_id"], "尚无讲解？")["result"]["question"]

    missing = prepare(config, question["question_id"])

    assert missing["status"] == "missing_input"
    assert show(config)["result"]["events"] == []

    config2, _, explained = explained_question(tmp_path / "second")
    token = prepare(config2, explained)["result"]["preparation_id"]
    skipped = record(config2, token, "skip-1", answer=None, answer_summary=None,
                     hints=[], model_evaluation="not_scored")
    assert skipped["result"]["event"]["effective_evaluation"] == "not_scored"


def test_review_rejects_pointer_path_traversal(tmp_path: Path) -> None:
    config, _, question_id = explained_question(tmp_path)
    prepare(config, question_id)
    pointer_path = config.results / "review/current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["commit_id"] = "../outside"
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

    with pytest.raises(WorkspaceError, match="pointer"):
        show(config)


def test_review_rejects_noncanonical_or_missing_pinned_object(tmp_path: Path) -> None:
    config, _, question_id = explained_question(tmp_path)
    preparation_id = prepare(config, question_id)["result"]["preparation_id"]
    rewrite_review_snapshot(
        config,
        lambda snapshot: snapshot["record"]["preparations"][preparation_id]["pin"].update(
            logical_path="learning/objects/../../outside"
        ),
    )

    with pytest.raises(WorkspaceError, match="validation|pinned explanation path"):
        show(config)

    config2, _, question_id2 = explained_question(tmp_path / "missing")
    prepare(config2, question_id2)
    learning_pointer = json.loads((config2.results / "learning/current.json").read_text(encoding="utf-8"))
    learning_snapshot = json.loads((config2.results / "learning/commits" /
                                    f'{learning_pointer["commit_id"]}.json').read_text(encoding="utf-8"))
    digest = next(iter(learning_snapshot["objects"]))
    (config2.results / "learning/objects" / digest[:2] / digest).unlink()

    with pytest.raises(WorkspaceError, match="missing or corrupt"):
        show(config2)


def test_review_rejects_orphaned_consumption_link(tmp_path: Path) -> None:
    config, _, question_id = explained_question(tmp_path)
    preparation_id = prepare(config, question_id)["result"]["preparation_id"]
    rewrite_review_snapshot(
        config,
        lambda snapshot: snapshot["record"]["preparations"][preparation_id].update(
            consumed_by="missing-event"
        ),
    )

    with pytest.raises(WorkspaceError, match="consumption link"):
        backup_entries(config)
