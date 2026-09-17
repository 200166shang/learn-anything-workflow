import json
import hashlib
from copy import deepcopy
from pathlib import Path

from test_learning_workflow import cli, create_root, register_source, write_workspace, _linear_inputs


def remove_source_root_from_current_snapshot(config: Path, source_question: str) -> None:
    pointer_path = config.parent / "results/learning/current.json"
    pointer = json.loads(pointer_path.read_text())
    manifest_path = config.parent / "results/learning/commits" / f'{pointer["commit_id"]}.json'
    snapshot = json.loads(manifest_path.read_text())
    record = snapshot["record"]
    source_thread = record["questions"][source_question]["thread_id"]
    source_module = record["threads"][source_thread]["module_id"]
    source_questions = {key for key, value in record["questions"].items() if value["thread_id"] == source_thread}
    record["modules"].pop(source_module)
    record["threads"].pop(source_thread)
    for key in source_questions: record["questions"].pop(key)
    record["relationships"] = {key: value for key, value in record["relationships"].items()
                               if value["thread_id"] != source_thread}
    record["feedbacks"] = {key: value for key, value in record["feedbacks"].items()
                           if value["question_id"] not in source_questions}
    record["preparations"] = {key: value for key, value in record["preparations"].items()
                              if value["question_id"] not in source_questions}
    record["explanations"] = {key: value for key, value in record["explanations"].items()
                              if value["root_question_id"] not in source_questions}
    manifest_path.write_text(json.dumps(snapshot))
    pointer["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    pointer_path.write_text(json.dumps(pointer))


def commit_root(tmp_path: Path, config: Path, source: dict, question_id: str, text: str = "") -> dict:
    _, prepared = cli("explanation", "prepare", "--question-id", question_id,
                      "--profile", "linear_transform", "--workspace", config, "--json")
    draft, evidence, review = _linear_inputs(tmp_path, source, prepared["result"]["required_marker"])
    if text:
        draft.write_text(draft.read_text() + "\n" + text + "\n")
    code, committed = cli("explanation", "commit", "--question-id", question_id,
                          "--draft", draft, "--evidence", evidence, "--teaching-review", review,
                          "--profile", "linear_transform", "--preparation-id",
                          prepared["result"]["preparation_id"], "--workspace", config, "--json")
    assert code == 0, committed
    return committed


def reuse_review(path: Path, target: str, *, local_context: str = "本项目采用列向量约定，因此这里复用列为基向量像的结论。") -> Path:
    path.write_text(json.dumps({
        "target_question_id": target,
        "concept": "线性变换的基向量像",
        "semantic_equivalence": "两处的线性变换均指固定基下的列向量映射",
        "coordinate_assumptions": "列向量、同一输入输出基",
        "applicability_conditions": ["映射是线性的", "不包含平移"],
        "citation_intent": "解释本项目矩阵列的含义，不继承目标根的理解状态",
        "local_context": local_context,
    }, ensure_ascii=False))
    return path


def test_cross_root_reuse_is_pinned_local_and_browsing_does_not_move_position(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace"); source = register_source(tmp_path, config)
    source_thread, source_question = create_root(tmp_path, config, source, "概念根")
    committed = commit_root(tmp_path, config, source, source_question)
    target_thread, target_question = create_root(tmp_path, config, source, "项目根")
    review_path = reuse_review(tmp_path / "reuse.json", source_question)

    code, prepared = cli("explanation", "prepare", "--question-id", target_question,
                         "--profile", "linear_transform", "--reuse-review", review_path,
                         "--workspace", config, "--json")
    assert code == 3, prepared
    draft, evidence, teaching = _linear_inputs(tmp_path, source, prepared["result"]["required_marker"])
    local_context = json.loads(review_path.read_text())["local_context"]
    draft.write_text(draft.read_text() + "\n" + local_context + "\n")
    code, reused = cli("explanation", "commit", "--question-id", target_question,
                       "--draft", draft, "--evidence", evidence, "--teaching-review", teaching,
                       "--profile", "linear_transform", "--preparation-id", prepared["result"]["preparation_id"],
                       "--workspace", config, "--json")
    assert code == 0, reused

    _, located = cli("learning", "locate", target_question, "--workspace", config, "--json")
    reference = located["result"]["cross_root_references"][0]
    assert reference["status"] == "current"
    assert reference["source"] == {
        "module_id": located["result"]["cross_root_references"][0]["source"]["module_id"],
        "thread_id": source_thread, "question_id": source_question,
        "explanation_id": committed["result"]["explanation"]["explanation_id"],
        "explanation_revision": 1,
        "section_id": committed["result"]["explanation"]["section_map"][source_question][0],
        "object_sha256": committed["result"]["explanation"]["object_sha256"],
    }
    assert reference["local_context"] == local_context
    _, source_shown = cli("learning", "thread", "show", source_thread, "--workspace", config, "--json")
    _, target_shown = cli("learning", "thread", "show", target_thread, "--workspace", config, "--json")
    assert source_shown["result"]["thread"]["current_question_id"] == source_question
    assert target_shown["result"]["thread"]["current_question_id"] == target_question


def test_reuse_detects_changed_target_and_requires_explicit_recheck(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace"); source = register_source(tmp_path, config)
    _, source_question = create_root(tmp_path, config, source, "概念根")
    commit_root(tmp_path, config, source, source_question)
    _, target_question = create_root(tmp_path, config, source, "项目根")
    review_path = reuse_review(tmp_path / "reuse.json", source_question)
    _, prepared = cli("explanation", "prepare", "--question-id", target_question,
                      "--profile", "linear_transform", "--reuse-review", review_path,
                      "--workspace", config, "--json")
    draft, evidence, teaching = _linear_inputs(tmp_path, source, prepared["result"]["required_marker"])
    draft.write_text(draft.read_text() + "\n" + json.loads(review_path.read_text())["local_context"] + "\n")
    cli("explanation", "commit", "--question-id", target_question, "--draft", draft,
        "--evidence", evidence, "--teaching-review", teaching, "--profile", "linear_transform",
        "--preparation-id", prepared["result"]["preparation_id"], "--workspace", config, "--json")

    commit_root(tmp_path, config, source, source_question, "结论适用条件现在进一步收窄。")
    _, located = cli("learning", "locate", target_question, "--workspace", config, "--json")
    reference = located["result"]["cross_root_references"][0]
    assert reference["status"] == "needs_review"
    assert reference["current_target"]["explanation_revision"] == 2
    assert reference["change"] in {"location_or_expression_changed", "conclusion_or_conditions_changed"}

    code, blocked = cli("explanation", "prepare", "--question-id", target_question,
                        "--profile", "linear_transform", "--workspace", config, "--json")
    assert code == 3
    assert blocked["result"]["explanation_state"] == "needs_review"
    assert blocked["result"]["cross_root_references"][0]["status"] == "needs_review"

    _, rechecked = cli("explanation", "prepare", "--question-id", target_question,
                       "--profile", "linear_transform", "--reuse-review", review_path,
                       "--workspace", config, "--json")
    revised, revised_evidence, revised_teaching = _linear_inputs(
        tmp_path, source, rechecked["result"]["required_marker"])
    revised.write_text(revised.read_text() + "\n" + json.loads(review_path.read_text())["local_context"] + "\n")
    code, refreshed = cli("explanation", "commit", "--question-id", target_question,
                          "--draft", revised, "--evidence", revised_evidence,
                          "--teaching-review", revised_teaching, "--profile", "linear_transform",
                          "--preparation-id", rechecked["result"]["preparation_id"],
                          "--workspace", config, "--json")
    assert code == 0, refreshed
    _, current = cli("learning", "locate", target_question, "--workspace", config, "--json")
    assert current["result"]["cross_root_references"][0]["status"] == "current"
    assert current["result"]["cross_root_references"][0]["source"]["explanation_revision"] == 2


def test_target_revision_race_preserves_conflict_instead_of_publishing_stale_pin(tmp_path: Path, monkeypatch) -> None:
    from video_extract import learning
    from video_extract.workspace import discover_workspace

    config = write_workspace(tmp_path / "workspace"); source = register_source(tmp_path, config)
    _, source_question = create_root(tmp_path, config, source, "概念根")
    commit_root(tmp_path, config, source, source_question)
    _, target_question = create_root(tmp_path, config, source, "项目根")
    workspace = discover_workspace(config)
    stale_snapshot = deepcopy(learning._load(workspace))
    commit_root(tmp_path, config, source, source_question, "并发修订目标结论。")
    real_load = learning._load
    calls = 0

    def stale_then_current(current_workspace):
        nonlocal calls
        calls += 1
        return stale_snapshot if calls == 1 else real_load(current_workspace)

    monkeypatch.setattr(learning, "_load", stale_then_current)
    result = learning.prepare_explanation(workspace, target_question, "linear_transform",
                                          json.loads(reuse_review(tmp_path / "race.json", source_question).read_text()))

    assert result["status"] == "awaiting_user"
    assert result["validation"]["cross_root_target"] == "conflict"
    assert Path(result["result"]["candidate"]).is_file()
    _, located = cli("learning", "locate", target_question, "--workspace", config, "--json")
    assert located["result"]["explanation_state"] == "pending"


def test_missing_cross_root_target_is_local_needs_review_not_snapshot_corruption(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace"); source = register_source(tmp_path, config)
    _, source_question = create_root(tmp_path, config, source, "概念根")
    commit_root(tmp_path, config, source, source_question)
    _, target_question = create_root(tmp_path, config, source, "项目根")
    review_path = reuse_review(tmp_path / "reuse.json", source_question)
    _, prepared = cli("explanation", "prepare", "--question-id", target_question,
                      "--profile", "linear_transform", "--reuse-review", review_path,
                      "--workspace", config, "--json")
    draft, evidence, teaching = _linear_inputs(tmp_path, source, prepared["result"]["required_marker"])
    draft.write_text(draft.read_text() + "\n" + json.loads(review_path.read_text())["local_context"] + "\n")
    cli("explanation", "commit", "--question-id", target_question, "--draft", draft,
        "--evidence", evidence, "--teaching-review", teaching, "--profile", "linear_transform",
        "--preparation-id", prepared["result"]["preparation_id"], "--workspace", config, "--json")
    remove_source_root_from_current_snapshot(config, source_question)

    code, located = cli("learning", "locate", target_question, "--workspace", config, "--json")
    assert code == 0, located
    assert located["result"]["explanation_state"] == "available"
    assert located["result"]["cross_root_references"][0]["status"] == "needs_review"
    assert located["result"]["cross_root_references"][0]["change"] == "target_missing"


def test_stale_reference_does_not_pollute_explanation_or_block_other_question_prepare(tmp_path: Path) -> None:
    config = write_workspace(tmp_path / "workspace"); source = register_source(tmp_path, config)
    _, source_question = create_root(tmp_path, config, source, "概念根")
    commit_root(tmp_path, config, source, source_question)
    target_thread, target_question = create_root(tmp_path, config, source, "项目根")
    review_path = reuse_review(tmp_path / "reuse.json", source_question)
    _, prepared = cli("explanation", "prepare", "--question-id", target_question,
                      "--profile", "linear_transform", "--reuse-review", review_path,
                      "--workspace", config, "--json")
    draft, evidence, teaching = _linear_inputs(tmp_path, source, prepared["result"]["required_marker"])
    draft.write_text(draft.read_text() + "\n" + json.loads(review_path.read_text())["local_context"] + "\n")
    cli("explanation", "commit", "--question-id", target_question, "--draft", draft,
        "--evidence", evidence, "--teaching-review", teaching, "--profile", "linear_transform",
        "--preparation-id", prepared["result"]["preparation_id"], "--workspace", config, "--json")
    _, child = cli("learning", "pursue", "--thread-id", target_thread, "--from-question-id", target_question,
                   "--relation", "deepens", "--question", "本根无关追问", "--workspace", config, "--json")
    commit_root(tmp_path, config, source, source_question, "目标变化。")

    _, located = cli("learning", "locate", target_question, "--workspace", config, "--json")
    assert located["result"]["explanation_state"] == "available"
    assert located["result"]["cross_root_references"][0]["status"] == "needs_review"
    code, unrelated = cli("explanation", "prepare", "--question-id", child["result"]["question"]["question_id"],
                          "--profile", "linear_transform", "--workspace", config, "--json")
    assert code == 3
    assert unrelated["status"] == "awaiting_model"
