import json
import os
import subprocess
import sys
from pathlib import Path

from video_extract.learning import (commit_explanation, create_module, create_thread,
                                    prepare_explanation, pursue, record_feedback, show_module)
from video_extract.learning_view import build_view, locate_view, status_view
from video_extract.source_registry import register
from video_extract.workspace import WorkspaceConfig


def workspace(tmp_path: Path) -> WorkspaceConfig:
    root = tmp_path / "workspace"
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
''', encoding="utf-8")
    return WorkspaceConfig.load(config)


def navigation_fixture(tmp_path: Path) -> tuple[WorkspaceConfig, str, str, str]:
    config = workspace(tmp_path)
    source_path = tmp_path / "course.md"
    source_path.write_text("# Camera\nCalibration, projection, and linear transforms.\n", encoding="utf-8")
    source = register(config, source_path)["result"]
    module = create_module(config, "理解视觉几何", "标定到投影", source["source_id"],
                           source["source_version"])["result"]["module"]
    rooted = create_thread(config, module["module_id"], "相机怎样标定？")["result"]
    root_id = rooted["question"]["question_id"]
    projection = pursue(config, rooted["thread"]["thread_id"], root_id, "deepens",
                         "三维点怎样投影到图像？")["result"]["question"]
    linear = pursue(config, rooted["thread"]["thread_id"], projection["question_id"], "deepens",
                     "线性变换是什么意思？")["result"]["question"]
    record_feedback(config, linear["question_id"], "parked", "先暂放",
                    "矩阵的列为什么表示基向量去向？")
    return config, module["module_id"], root_id, linear["question_id"]


def test_builds_rebuildable_generation_with_position_route_feedback_and_pending_nodes(tmp_path: Path) -> None:
    config, module_id, root_id, current_id = navigation_fixture(tmp_path)

    built = build_view(config, module_id)

    assert built["status"] == "completed"
    generation = Path(built["result"]["generation_path"])
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["learning_commit_id"].startswith("learning-commit-")
    assert manifest["root_digest"]
    assert manifest["module_id"] == module_id
    assert manifest["current_question_id"] == current_id
    assert manifest["nodes"][current_id]["feedback"] == "parked"
    assert manifest["nodes"][current_id]["content_state"] == "pending"
    assert manifest["nodes"][root_id]["is_current"] is False
    assert manifest["edges"][0]["kind"] == "deepens"
    html = (generation / "局部问题图.html").read_text(encoding="utf-8")
    assert "续学位置" in html and "本次返回路线" in html and "暂放" in html
    assert "跨根引用" in html and "点击仅查看，不改学习状态" in html

    state = status_view(config, module_id)
    assert state["result"]["sync_state"] == "current"
    pending = locate_view(config, current_id)
    assert pending["status"] == "awaiting_model"
    assert pending["result"]["content_state"] == "pending"

    # A deleted derived generation can be rebuilt without changing authoritative learning state.
    learning_commit = manifest["learning_commit_id"]
    for path in sorted(generation.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    generation.rmdir()
    rebuilt = build_view(config, module_id)
    assert rebuilt["status"] == "completed"
    assert json.loads(Path(rebuilt["result"]["manifest_path"]).read_text())["learning_commit_id"] == learning_commit


def test_failed_build_keeps_previous_generation_and_reports_unsynced(tmp_path: Path, monkeypatch) -> None:
    config, module_id, _, current_id = navigation_fixture(tmp_path)
    first = build_view(config, module_id)
    pointer = Path(first["result"]["pointer_path"])
    previous = pointer.read_bytes()
    record_feedback(config, current_id, "confused", "还是不理解")
    monkeypatch.setenv("VIDEO_EXTRACT_VIEW_TEST_FAULT", "before_publish")

    failed = build_view(config, module_id)

    assert failed["status"] == "recoverable_failure"
    assert pointer.read_bytes() == previous
    state = status_view(config, module_id)
    assert state["result"]["sync_state"] == "unsynced"
    assert state["result"]["served_generation"] == first["result"]["generation_id"]


def test_rebuild_preserves_manual_change_and_marks_generation_unsynced(tmp_path: Path) -> None:
    config, module_id, _, _ = navigation_fixture(tmp_path)
    first = build_view(config, module_id)
    generation = Path(first["result"]["generation_path"])
    graph = generation / "局部问题图.html"
    graph.write_text(graph.read_text(encoding="utf-8") + "\n人工修改\n", encoding="utf-8")

    rebuilt = build_view(config, module_id)

    assert rebuilt["status"] == "recoverable_failure"
    assert "人工修改" in graph.read_text(encoding="utf-8")
    assert status_view(config, module_id)["result"]["sync_state"] == "unsynced"


def test_status_and_locate_reject_a_modified_served_generation_without_writing_learning_state(
        tmp_path: Path) -> None:
    config, module_id, root_id, _ = navigation_fixture(tmp_path)
    built = build_view(config, module_id)
    learning_root = config.results / "learning"
    before = {path.relative_to(learning_root): path.read_bytes()
              for path in learning_root.rglob("*") if path.is_file()}
    graph = Path(built["result"]["generation_path"]) / "局部问题图.html"
    graph.write_text(graph.read_text(encoding="utf-8") + "\n未授权修改\n", encoding="utf-8")

    state = status_view(config, module_id)
    located = locate_view(config, root_id)

    assert state["status"] == "completed"
    assert state["result"]["sync_state"] == "unsynced"
    assert located["status"] == "recoverable_failure"
    assert located["result"]["content_state"] == "unsynced"
    after = {path.relative_to(learning_root): path.read_bytes()
             for path in learning_root.rglob("*") if path.is_file()}
    assert after == before


def test_status_reports_unsynced_for_a_schema_invalid_generation_manifest(tmp_path: Path) -> None:
    config, module_id, _, _ = navigation_fixture(tmp_path)
    built = build_view(config, module_id)
    manifest = Path(built["result"]["manifest_path"])
    manifest.write_text("{}\n", encoding="utf-8")

    state = status_view(config, module_id)

    assert state["status"] == "completed"
    assert state["result"]["sync_state"] == "unsynced"


def test_click_target_is_complete_explanation_at_exact_stable_section(tmp_path: Path) -> None:
    config, module_id, root_id, _ = navigation_fixture(tmp_path)
    source_ref = show_module(config, module_id)["result"]["module"]["source_refs"][0]
    prepared = prepare_explanation(config, root_id, "linear_transform")["result"]
    marker = prepared["required_marker"]
    draft = tmp_path / "explanation.md"
    draft.write_text(f'''# 相机标定与线性变换
{marker}
直觉：标定是在寻找坐标关系。因果：基向量的去向决定任意线性组合的去向。
机制：矩阵的列分别记录两组基向量的像。
例子：矩阵 [[2,0],[0,3]] 把 (1,2) 变为 (2,6)。
条件与边界：平移不是线性变换，必须使用仿射或齐次坐标；坐标依赖所选基。

这是完整讲解后半部分，不是短预览。
''', encoding="utf-8")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps([{"source_id": source_ref["source_id"],
                                     "source_version": source_ref["source_version"],
                                     "locator": {"kind": "heading", "value": "Camera"},
                                     "claim_type": "course_fact", "claim": "课程连接标定与投影"}]), encoding="utf-8")
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"intuition": True, "causality": True, "mechanism": True,
                                  "worked_example": True, "conditions": True,
                                  "source_alignment": True, "basis_coordinate_reasoning": True,
                                  "affine_boundary": True}), encoding="utf-8")
    committed = commit_explanation(config, root_id, draft, evidence, review, "linear_transform",
                                   prepared["preparation_id"])
    assert committed["status"] == "completed"

    build_view(config, module_id)
    located = locate_view(config, root_id)

    assert located["status"] == "completed"
    assert located["result"]["obsidian_href"].endswith(f'#^{prepared["section_id"]}')
    rendered = Path(located["result"]["document_path"]).read_text(encoding="utf-8")
    assert f'^{prepared["section_id"]}' in rendered
    assert "这是完整讲解后半部分" in rendered


def test_merged_explanation_projects_every_stable_section_into_one_document(tmp_path: Path) -> None:
    config, module_id, root_id, followup_id = navigation_fixture(tmp_path)
    source_ref = show_module(config, module_id)["result"]["module"]["source_refs"][0]
    prepared = prepare_explanation(config, root_id, "linear_transform")["result"]
    root_section = prepared["section_id"]
    followup_section = "section-22222222-2222-4222-8222-222222222222"
    draft = tmp_path / "merged.md"
    draft.write_text(f'''# 合并讲解
<!-- section-id: {root_section} -->
直觉与因果机制：标定建立坐标关系，基向量去向决定矩阵列。
例子：矩阵 [[2,0],[0,3]] 把 (1,2) 变为 (2,6)。条件与边界：平移需要仿射或齐次坐标。
<!-- section-id: {followup_section} -->
线性变换的坐标依赖所选基，每列记录对应基向量的像。
''', encoding="utf-8")
    evidence = tmp_path / "merged-evidence.json"
    evidence.write_text(json.dumps([{"source_id": source_ref["source_id"],
                                     "source_version": source_ref["source_version"],
                                     "locator": {"kind": "heading", "value": "Camera"},
                                     "claim_type": "course_fact", "claim": "课程连接标定与投影"}]))
    review = tmp_path / "merged-review.json"
    review.write_text(json.dumps({"intuition": True, "causality": True, "mechanism": True,
                                  "worked_example": True, "conditions": True,
                                  "source_alignment": True, "basis_coordinate_reasoning": True,
                                  "affine_boundary": True}))
    section_map = tmp_path / "section-map.json"
    section_map.write_text(json.dumps({root_id: [root_section], followup_id: [followup_section]}))
    metadata = tmp_path / "revision-metadata.json"
    metadata.write_text(json.dumps({"summary": "合并同根历史问题的完整讲解",
                                    "affected_question_ids": [root_id, followup_id]}))

    committed = commit_explanation(config, root_id, draft, evidence, review, "linear_transform",
                                   prepared["preparation_id"], section_map_path=section_map,
                                   revision_metadata_path=metadata)
    assert committed["status"] == "completed"

    built = build_view(config, module_id)

    assert built["status"] == "completed"
    root_location = locate_view(config, root_id)
    followup_location = locate_view(config, followup_id)
    assert root_location["status"] == followup_location["status"] == "completed"
    assert root_location["result"]["document_path"] == followup_location["result"]["document_path"]
    rendered = Path(root_location["result"]["document_path"]).read_text(encoding="utf-8")
    assert f"^{root_section}" in rendered
    assert f"^{followup_section}" in rendered


def test_project_ships_unloadable_read_only_obsidian_plugin() -> None:
    plugin = Path(__file__).parents[1] / "integrations" / "obsidian-learning-map"
    manifest = json.loads((plugin / "manifest.json").read_text(encoding="utf-8"))
    main = (plugin / "main.js").read_text(encoding="utf-8")
    assert manifest["id"] == "video-extract-learning-map"
    assert "registerView" in main
    assert "registerEvent" in main
    assert "openLinkText" in main
    assert "onunload" in main
    assert ".modify(" not in main and ".create(" not in main


def test_view_cli_exposes_build_status_and_exact_locate(tmp_path: Path) -> None:
    config, module_id, _, current_id = navigation_fixture(tmp_path)
    root = Path(__file__).parents[1]

    def run(*args: str) -> tuple[int, dict]:
        done = subprocess.run([sys.executable, "-m", "video_extract.cli", *args,
                              "--workspace", str(config.config_path), "--json"],
                              cwd=root, capture_output=True, text=True)
        return done.returncode, json.loads(done.stdout)

    code, built = run("view", "build", "--module-id", module_id)
    assert code == 0 and built["status"] == "completed"
    code, state = run("view", "status", "--module-id", module_id)
    assert code == 0 and state["result"]["sync_state"] == "current"
    code, located = run("view", "locate", current_id)
    assert code == 3 and located["result"]["content_state"] == "pending"
