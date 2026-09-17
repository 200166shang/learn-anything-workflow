import json
import os
import subprocess
import sys
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


def register_source(tmp_path: Path, config: Path, content: str = "# Vectors\n") -> dict:
    source = tmp_path / "source.md"
    source.write_text(content, encoding="utf-8")
    code, result = cli("source", "register", source, "--workspace", config, "--json")
    assert code == 0
    return result["result"]


def test_create_and_show_module_with_confirmed_goal_scope_and_source_version(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = register_source(tmp_path, config)

    code, created = cli(
        "learning", "module", "create", "--goal", "理解线性变换",
        "--scope", "矩阵如何作用于二维向量", "--source-id", source["source_id"],
        "--source-version", source["source_version"], "--workspace", config, "--json",
    )

    assert code == 0
    assert created["status"] == "completed"
    module = created["result"]["module"]
    assert module["module_id"].startswith("module-")
    assert module["goal"] == "理解线性变换"
    assert module["scope"] == "矩阵如何作用于二维向量"
    assert module["source_refs"] == [{"source_id": source["source_id"],
                                      "source_version": source["source_version"]}]
    assert module["thread_ids"] == []
    assert created["result"]["learning_schema_version"] == 2
    assert created["result"]["revision"] == 1

    code, shown = cli("learning", "module", "show", module["module_id"],
                      "--workspace", config, "--json")
    assert code == 0
    assert shown["result"]["module"] == module
    assert shown["result"]["commit_id"] == created["result"]["commit_id"]


def test_root_recommendation_pauses_for_model_without_creating_history(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = register_source(tmp_path, config, "# Basis\nA matrix maps basis vectors.\n")
    _, created = cli("learning", "module", "create", "--goal", "理解线性变换",
                     "--scope", "矩阵列", "--source-id", source["source_id"],
                     "--source-version", source["source_version"], "--workspace", config, "--json")
    module_id = created["result"]["module"]["module_id"]

    code, recommendation = cli("learning", "recommend", "--module-id", module_id,
                               "--workspace", config, "--json")

    assert code == 3
    assert recommendation["status"] == "awaiting_model"
    assert recommendation["next_action"]["type"] == "model"
    assert recommendation["next_action"]["action"] == "recommend_root_questions"
    assert recommendation["result"]["source_context"][0]["content"].startswith("# Basis")
    _, shown = cli("learning", "module", "show", module_id, "--workspace", config, "--json")
    assert shown["result"]["module"]["thread_ids"] == []
    assert shown["result"]["commit_id"] == created["result"]["commit_id"]


def test_selected_root_and_actual_followup_are_saved_before_explanations(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = register_source(tmp_path, config)
    _, created = cli("learning", "module", "create", "--goal", "理解矩阵", "--scope", "二维",
                     "--source-id", source["source_id"], "--source-version", source["source_version"],
                     "--workspace", config, "--json")
    module_id = created["result"]["module"]["module_id"]

    code, rooted = cli("learning", "thread", "create", "--module-id", module_id,
                       "--root-question", "为什么矩阵列代表基向量的去向？",
                       "--workspace", config, "--json")
    assert code == 0
    thread = rooted["result"]["thread"]
    root = rooted["result"]["question"]
    assert thread["module_id"] == module_id
    assert thread["root_question_id"] == root["question_id"]
    assert thread["current_question_id"] == root["question_id"]
    assert root["original_question"] == "为什么矩阵列代表基向量的去向？"
    assert root["explanation_refs"] == []

    code, pursued = cli("learning", "pursue", "--thread-id", thread["thread_id"],
                        "--from-question-id", root["question_id"], "--relation", "deepens",
                        "--question", "坐标为什么也必须相对一组基？", "--workspace", config, "--json")
    assert code == 0
    followup = pursued["result"]["question"]
    assert followup["question_id"] != root["question_id"]
    assert followup["original_question"] == "坐标为什么也必须相对一组基？"
    assert followup["explanation_refs"] == []
    assert pursued["result"]["relationship"]["type"] == "deepens"

    code, shown = cli("learning", "thread", "show", thread["thread_id"],
                      "--workspace", config, "--json")
    assert code == 0
    assert shown["result"]["thread"]["current_question_id"] == followup["question_id"]
    assert {item["question_id"] for item in shown["result"]["questions"]} == {
        root["question_id"], followup["question_id"]}

    code, located = cli("learning", "locate", followup["question_id"],
                        "--workspace", config, "--json")
    assert code == 3
    assert located["status"] == "awaiting_model"
    assert located["result"]["explanation_state"] == "pending"


def create_root(tmp_path: Path, config: Path, source: dict, question: str) -> tuple[str, str]:
    _, module = cli("learning", "module", "create", "--goal", "学习", "--scope", "当前资料",
                    "--source-id", source["source_id"], "--source-version", source["source_version"],
                    "--workspace", config, "--json")
    _, rooted = cli("learning", "thread", "create", "--module-id", module["result"]["module"]["module_id"],
                    "--root-question", question, "--workspace", config, "--json")
    return rooted["result"]["thread"]["thread_id"], rooted["result"]["question"]["question_id"]


def test_prepare_and_commit_versioned_source_grounded_linear_transform_explanation(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = register_source(tmp_path, config, "# Linear maps\nColumns are images of basis vectors.\n")
    thread_id, question_id = create_root(tmp_path, config, source, "为什么矩阵列代表基向量去向？")

    code, prepared = cli("explanation", "prepare", "--question-id", question_id,
                         "--profile", "linear_transform", "--workspace", config, "--json")
    assert code == 3
    assert prepared["status"] == "awaiting_model"
    section_id = prepared["result"]["section_id"]
    preparation_id = prepared["result"]["preparation_id"]
    assert section_id.startswith("section-")
    assert prepared["result"]["required_marker"] == f"<!-- section-id: {section_id} -->"
    assert prepared["result"]["source_context"][0]["source_version"] == source["source_version"]

    draft = tmp_path / "explanation.md"
    draft.write_text(f'''# 线性变换
<!-- section-id: {section_id} -->
直觉：矩阵描述空间如何改变。因果上，任意向量由基向量线性组合而成。
机制：矩阵两列分别是 (1,0) 与 (0,1) 的去向。
例子：[[2,0],[0,3]] 把 (1,2) 变成 (2,6)。
条件与边界：坐标依赖所选基；平移不是线性变换，需仿射或齐次坐标。
''', encoding="utf-8")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps([{"source_id": source["source_id"],
                                     "source_version": source["source_version"],
                                     "locator": {"kind": "heading", "value": "Linear maps"},
                                     "claim_type": "course_fact", "claim": "矩阵列是基向量的像"}]), encoding="utf-8")
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"intuition": True, "causality": True, "mechanism": True,
                                  "worked_example": True, "conditions": True,
                                  "source_alignment": True, "basis_coordinate_reasoning": True,
                                  "affine_boundary": True}), encoding="utf-8")

    code, committed = cli("explanation", "commit", "--question-id", question_id,
                          "--draft", draft, "--evidence", evidence, "--teaching-review", review,
                          "--profile", "linear_transform", "--preparation-id", preparation_id,
                          "--workspace", config, "--json")
    assert code == 0
    explanation = committed["result"]["explanation"]
    assert explanation["explanation_id"].startswith("explanation-")
    assert explanation["revision"] == 1
    assert explanation["evidence_refs"][0]["source_version"] == source["source_version"]
    assert explanation["section_map"][question_id] == [section_id]

    code, replayed = cli("explanation", "commit", "--question-id", question_id,
                         "--draft", draft, "--evidence", evidence, "--teaching-review", review,
                         "--profile", "linear_transform", "--preparation-id", preparation_id,
                         "--workspace", config, "--json")
    assert code == 1
    assert replayed["validation"]["preparation"] == "failed"

    code, located = cli("learning", "locate", question_id, "--workspace", config, "--json")
    assert code == 0
    assert located["result"]["locations"][0]["section_id"] == section_id
    assert located["result"]["locations"][0]["explanation_revision"] == 1
    assert Path(located["result"]["locations"][0]["document_path"]).read_text(encoding="utf-8") == draft.read_text(encoding="utf-8")

    _, prepared_update = cli("explanation", "prepare", "--question-id", question_id,
                             "--profile", "linear_transform", "--workspace", config, "--json")
    update_token = prepared_update["result"]["preparation_id"]
    update_marker = prepared_update["result"]["required_marker"]
    bad = tmp_path / "bad.md"; bad.write_text("# Thin answer\n", encoding="utf-8")
    code, rejected = cli("explanation", "commit", "--question-id", question_id,
                         "--draft", bad, "--evidence", evidence, "--teaching-review", review,
                         "--profile", "linear_transform", "--preparation-id", update_token,
                         "--workspace", config, "--json")
    assert code == 1
    assert rejected["status"] == "failed"
    _, still_located = cli("learning", "locate", question_id, "--workspace", config, "--json")
    assert still_located["result"]["locations"][0]["explanation_revision"] == 1

    first_path = Path(still_located["result"]["locations"][0]["document_path"])
    revised = draft.read_text(encoding="utf-8").replace(
        f"<!-- section-id: {section_id} -->", update_marker
    ) + "\n补充边界：非线性函数不能由固定矩阵表示。\n"
    draft.write_text(revised, encoding="utf-8")
    code, updated = cli("explanation", "commit", "--question-id", question_id,
                        "--draft", draft, "--evidence", evidence, "--teaching-review", review,
                        "--profile", "linear_transform", "--preparation-id", update_token,
                        "--workspace", config, "--json")
    assert code == 0
    assert updated["result"]["explanation"]["explanation_id"] == explanation["explanation_id"]
    assert updated["result"]["explanation"]["revision"] == 2
    assert first_path.is_file()
    _, revised_location = cli("learning", "locate", question_id, "--workspace", config, "--json")
    assert revised_location["result"]["locations"][0]["explanation_revision"] == 2


def test_stale_learning_revision_preserves_a_conflict_candidate(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = register_source(tmp_path, config)
    thread_id, root_id = create_root(tmp_path, config, source, "根问题")

    code, conflict = cli("learning", "pursue", "--thread-id", thread_id,
                         "--from-question-id", root_id, "--relation", "deepens",
                         "--question", "不会覆盖的问题", "--expected-revision", 0,
                         "--workspace", config, "--json")

    assert code == 3
    assert conflict["status"] == "awaiting_user"
    assert conflict["validation"]["expected_revision"] == "conflict"
    candidate = Path(conflict["result"]["candidate"])
    assert candidate.is_file()
    assert json.loads(candidate.read_text(encoding="utf-8"))["proposal"]["question"] == "不会覆盖的问题"
    _, shown = cli("learning", "thread", "show", thread_id, "--workspace", config, "--json")
    assert [item["question_id"] for item in shown["result"]["questions"]] == [root_id]


@pytest.mark.parametrize(("profile", "text"), [
    ("recognition_to_action", "直觉与因果：检测/识别结果携带坐标，坐标转换后进入决策，再由执行器执行 action。机制例子处理过期 stale 数据，当前代码事实与设计建议分开。条件与边界明确。"),
    ("frame_pipeline", "直觉与因果：入口 entry 接收一帧；producer 生产者放入 queue 队列，consumer 消费者在线程异步边界处理，结果从 output 出口返回。机制例子说明慢、队列满和退出 shutdown。条件与边界明确。"),
])
def test_source_grounded_teaching_profiles_have_executable_semantic_checks(
        tmp_path: Path, profile: str, text: str) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = register_source(tmp_path, config, "# Implementation\nObserved behavior.\n")
    _, question_id = create_root(tmp_path, config, source, "实际链路如何工作？")
    _, prepared = cli("explanation", "prepare", "--question-id", question_id,
                      "--profile", profile, "--workspace", config, "--json")
    marker = prepared["result"]["required_marker"]
    preparation_id = prepared["result"]["preparation_id"]
    draft = tmp_path / f"{profile}.md"; draft.write_text(f"# Explanation\n{marker}\n{text}\n", encoding="utf-8")
    evidence = tmp_path / "evidence.json"; evidence.write_text(json.dumps([{
        "source_id": source["source_id"], "source_version": source["source_version"],
        "locator": {"kind": "heading", "value": "Implementation"},
        "claim_type": "course_fact", "claim": "Observed source fact"}]), encoding="utf-8")
    profile_review = ({"actual_code_separated": True, "stale_input_handling": True}
                      if profile == "recognition_to_action" else
                      {"actual_entry_verified": True, "concurrency_verified": True,
                       "backpressure_shutdown": True})
    review = tmp_path / "review.json"; review.write_text(json.dumps({
        "intuition": True, "causality": True, "mechanism": True, "worked_example": True,
        "conditions": True, "source_alignment": True, **profile_review}), encoding="utf-8")

    code, committed = cli("explanation", "commit", "--question-id", question_id,
                          "--draft", draft, "--evidence", evidence, "--teaching-review", review,
                          "--profile", profile, "--preparation-id", preparation_id,
                          "--workspace", config, "--json")

    assert code == 0
    assert committed["validation"]["teaching_quality"] == "passed"


def test_after_publish_failure_is_reconciled_without_repeating_learning_write(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = register_source(tmp_path, config)

    code, uncertain = cli("learning", "module", "create", "--goal", "学习", "--scope", "一章",
                          "--source-id", source["source_id"], "--source-version", source["source_version"],
                          "--workspace", config, "--json",
                          env={"VIDEO_EXTRACT_LEARNING_TEST_FAULT": "after_publish"})
    assert code == 1
    assert uncertain["status"] == "recoverable_failure"
    assert uncertain["result"]["logical_visibility"] == "current"
    commit_id = uncertain["result"]["commit_id"]

    code, reconciled = cli("learning", "reconcile", "--commit-id", commit_id,
                           "--workspace", config, "--json")
    assert code == 0
    assert reconciled["result"]["durability"] == "confirmed"
    assert reconciled["result"]["revision"] == 1


def test_code_source_can_prepare_learning_without_media_or_source_notes(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    code_root = tmp_path / "code"; code_root.mkdir(); (code_root / "pipeline.py").write_text(
        "def consume(frame):\n    return frame\n", encoding="utf-8")
    status, registered = cli("source", "register", code_root, "--workspace", config, "--json")
    assert status == 0
    source = registered["result"]
    _, question_id = create_root(tmp_path, config, source, "一帧从哪里进入和离开？")

    status, prepared = cli("explanation", "prepare", "--question-id", question_id,
                           "--profile", "frame_pipeline", "--workspace", config, "--json")

    assert status == 3
    context = prepared["result"]["source_context"][0]
    assert context["kind"] == "code"
    assert context["availability"] == "available_at_location"
    assert context["location"] == str(code_root.resolve())


def test_learning_store_rejects_a_symlink_escape_from_results(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = register_source(tmp_path, config)
    results = config.parent / "results"; results.mkdir(exist_ok=True)
    outside = tmp_path / "outside"; outside.mkdir()
    (results / "learning").symlink_to(outside, target_is_directory=True)

    code, rejected = cli("learning", "module", "create", "--goal", "学习", "--scope", "一章",
                         "--source-id", source["source_id"], "--source-version", source["source_version"],
                         "--workspace", config, "--json")

    assert code == 1
    assert rejected["status"] == "failed"
    assert "symbolic link" in rejected["diagnostics"][0]
    assert not any(outside.iterdir())


def test_learning_capability_contract_checks_and_runs_the_same_public_workflow(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = register_source(tmp_path, config)
    code, checked = cli("capability", "check", "learning.learn", "--json")
    assert code == 0
    assert checked["result"]["capabilities"][0]["contract_state"] == "compatible"
    request = tmp_path / "learning-request.json"
    request.write_text(json.dumps({
        "contract_version": 1, "action": "module.create", "workspace": str(config),
        "goal": "理解资料", "scope": "第一章", "source_id": source["source_id"],
        "source_version": source["source_version"],
    }), encoding="utf-8")

    code, result = cli("capability", "run", "learning.learn", "--request", request, "--json")

    assert code == 0
    assert result["status"] == "completed"
    assert result["provenance"]["capability_id"] == "learning.learn"
    assert result["result"]["module"]["goal"] == "理解资料"


def test_deep_validation_rejects_cross_owned_thread_even_when_json_schema_is_valid(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    source = register_source(tmp_path, config)
    thread_id, _ = create_root(tmp_path, config, source, "根问题")
    results = config.parent / "results"; pointer_path = results / "learning/current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    manifest_path = results / "learning/commits" / f'{pointer["commit_id"]}.json'
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["record"]["threads"][thread_id]["module_id"] = "module-22222222-2222-4222-8222-222222222222"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    pointer["manifest_sha256"] = __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest()
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

    code, rejected = cli("learning", "thread", "show", thread_id, "--workspace", config, "--json")

    assert code == 1
    assert rejected["status"] == "failed"
    assert "ownership" in rejected["diagnostics"][0] or "module reference" in rejected["diagnostics"][0]


def test_missing_code_source_blocks_only_its_module_with_failed_source_validation(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    code_root = tmp_path / "code"; code_root.mkdir(); (code_root / "main.py").write_text("x = 1\n")
    _, registered = cli("source", "register", code_root, "--workspace", config, "--json")
    source = registered["result"]
    _, module = cli("learning", "module", "create", "--goal", "读代码", "--scope", "main",
                    "--source-id", source["source_id"], "--source-version", source["source_version"],
                    "--workspace", config, "--json")
    module_id = module["result"]["module"]["module_id"]
    _, rooted = cli("learning", "thread", "create", "--module-id", module_id,
                    "--root-question", "它如何工作？", "--workspace", config, "--json")
    question_id = rooted["result"]["question"]["question_id"]
    code_root.rename(tmp_path / "gone")

    for arguments in (("learning", "recommend", "--module-id", module_id),
                      ("explanation", "prepare", "--question-id", question_id, "--profile", "frame_pipeline")):
        code, blocked = cli(*arguments, "--workspace", config, "--json")
        assert code == 3
        assert blocked["status"] == "missing_input"
        assert blocked["validation"]["sources"] == "failed"
        assert blocked["result"]["blocked_source_errors"]
    _, still = cli("learning", "thread", "show", rooted["result"]["thread"]["thread_id"],
                   "--workspace", config, "--json")
    assert still["status"] == "completed"


def test_capability_forwards_expected_revision_and_normalizes_publish_recovery(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace"); source = register_source(tmp_path, config)
    _, first = cli("learning", "module", "create", "--goal", "一", "--scope", "一",
                   "--source-id", source["source_id"], "--source-version", source["source_version"],
                   "--workspace", config, "--json")
    request = tmp_path / "stale.json"; request.write_text(json.dumps({
        "contract_version": 1, "action": "module.create", "workspace": str(config),
        "goal": "二", "scope": "二", "source_id": source["source_id"],
        "source_version": source["source_version"], "expected_revision": 0}), encoding="utf-8")
    code, conflict = cli("capability", "run", "learning.learn", "--request", request, "--json")
    assert code == 3 and conflict["status"] == "awaiting_user"
    assert Path(conflict["result"]["candidate"]).is_file()

    recovery = tmp_path / "recovery.json"; recovery.write_text(json.dumps({
        "contract_version": 1, "action": "module.create", "workspace": str(config),
        "goal": "三", "scope": "三", "source_id": source["source_id"],
        "source_version": source["source_version"], "expected_revision": first["result"]["revision"]}), encoding="utf-8")
    code, uncertain = cli("capability", "run", "learning.learn", "--request", recovery, "--json",
                          env={"VIDEO_EXTRACT_LEARNING_TEST_FAULT": "after_publish"})
    assert code == 1 and uncertain["status"] == "recoverable_failure"
    assert uncertain["next_action"]["type"] == "reconcile"
    assert uncertain["operation_id"].startswith("operation-")


def test_candidate_sync_failure_does_not_claim_a_preserved_candidate(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace"); source = register_source(tmp_path, config)
    thread_id, root_id = create_root(tmp_path, config, source, "根")
    code, failed = cli("learning", "pursue", "--thread-id", thread_id,
                       "--from-question-id", root_id, "--relation", "deepens", "--question", "追问",
                       "--expected-revision", 0, "--workspace", config, "--json",
                       env={"VIDEO_EXTRACT_LEARNING_TEST_FAULT": "candidate_sync"})
    assert code == 1 and failed["status"] == "failed"
    assert "candidate directory sync failure" in failed["diagnostics"][0]
    candidates = config.parent / "results/learning/candidates"
    assert not list(candidates.glob("candidate-*.json"))


def _linear_inputs(tmp_path: Path, source: dict, marker: str) -> tuple[Path, Path, Path]:
    draft = tmp_path / f"draft-{__import__('uuid').uuid4()}.md"
    draft.write_text(f"# 解释\n{marker}\n直觉和因果机制：基向量决定矩阵列。例子 (1,2) 变 (2,6)。条件边界：平移需要仿射或齐次坐标。\n")
    evidence = tmp_path / f"evidence-{__import__('uuid').uuid4()}.json"
    evidence.write_text(json.dumps([{"source_id": source["source_id"], "source_version": source["source_version"],
                                     "locator": {"kind": "heading", "value": "Vectors"},
                                     "claim_type": "course_fact", "claim": "source claim"}]))
    review = tmp_path / f"review-{__import__('uuid').uuid4()}.json"
    review.write_text(json.dumps({"intuition": True, "causality": True, "mechanism": True,
                                  "worked_example": True, "conditions": True, "source_alignment": True,
                                  "basis_coordinate_reasoning": True, "affine_boundary": True}))
    return draft, evidence, review


def test_preparation_token_is_bound_to_question_and_confirmed_source_scope(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace")
    source_one = register_source(tmp_path, config, "# Vectors\nOne\n")
    second_path = tmp_path / "second.md"; second_path.write_text("# Other\nTwo\n")
    _, second_registered = cli("source", "register", second_path, "--workspace", config, "--json")
    source_two = second_registered["result"]
    thread_id, first_question = create_root(tmp_path, config, source_one, "第一个问题")
    _, pursued = cli("learning", "pursue", "--thread-id", thread_id, "--from-question-id", first_question,
                     "--relation", "deepens", "--question", "第二个问题", "--workspace", config, "--json")
    second_question = pursued["result"]["question"]["question_id"]
    _, prepared = cli("explanation", "prepare", "--question-id", first_question,
                      "--profile", "linear_transform", "--workspace", config, "--json")
    draft, evidence, review = _linear_inputs(tmp_path, source_one, prepared["result"]["required_marker"])

    code, cross_question = cli("explanation", "commit", "--question-id", second_question,
                               "--draft", draft, "--evidence", evidence, "--teaching-review", review,
                               "--profile", "linear_transform", "--preparation-id", prepared["result"]["preparation_id"],
                               "--workspace", config, "--json")
    assert code == 1 and cross_question["validation"]["preparation"] == "failed"

    evidence.write_text(json.dumps([{"source_id": source_two["source_id"], "source_version": source_two["source_version"],
                                     "locator": {"kind": "heading", "value": "Other"},
                                     "claim_type": "course_fact", "claim": "out of scope"}]))
    code, cross_scope = cli("explanation", "commit", "--question-id", first_question,
                            "--draft", draft, "--evidence", evidence, "--teaching-review", review,
                            "--profile", "linear_transform", "--preparation-id", prepared["result"]["preparation_id"],
                            "--workspace", config, "--json")
    assert code == 1 and cross_scope["validation"]["source_scope"] == "failed"

    cli("learning", "module", "create", "--goal", "并发模块", "--scope", "并发范围",
        "--source-id", source_one["source_id"], "--source-version", source_one["source_version"],
        "--workspace", config, "--json")
    _, correct_evidence, _ = _linear_inputs(tmp_path, source_one, prepared["result"]["required_marker"])
    code, expired = cli("explanation", "commit", "--question-id", first_question,
                        "--draft", draft, "--evidence", correct_evidence, "--teaching-review", review,
                        "--profile", "linear_transform", "--preparation-id", prepared["result"]["preparation_id"],
                        "--workspace", config, "--json")
    assert code == 1 and expired["validation"]["preparation"] == "failed"


def test_locator_resolves_from_digest_after_results_root_moves(tmp_path: Path) -> None:
    first_root = tmp_path / "first"; config = write_workspace(first_root)
    source = register_source(tmp_path, config); _, question_id = create_root(tmp_path, config, source, "为什么？")
    _, prepared = cli("explanation", "prepare", "--question-id", question_id,
                      "--profile", "linear_transform", "--workspace", config, "--json")
    draft, evidence, review = _linear_inputs(tmp_path, source, prepared["result"]["required_marker"])
    code, _ = cli("explanation", "commit", "--question-id", question_id, "--draft", draft,
                  "--evidence", evidence, "--teaching-review", review, "--profile", "linear_transform",
                  "--preparation-id", prepared["result"]["preparation_id"], "--workspace", config, "--json")
    assert code == 0
    manifest_text = next((first_root / "results/learning/commits").glob("*.json")).read_text()
    assert str(first_root) not in manifest_text
    second_root = tmp_path / "moved"; first_root.rename(second_root); moved_config = second_root / "workspace.toml"

    code, located = cli("learning", "locate", question_id, "--workspace", moved_config, "--json")
    assert code == 0
    location = located["result"]["locations"][0]
    assert location["logical_path"].startswith("learning/objects/")
    assert Path(location["document_path"]).is_file()
    assert str(second_root / "results") in location["document_path"]
