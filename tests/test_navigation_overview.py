"""T14 module overview, searchable catalog, and cross-root navigation."""

import hashlib
import json
from pathlib import Path

from test_cross_root_reuse import commit_root, reuse_review
from test_learning_view import navigation_fixture
from test_learning_workflow import _linear_inputs, cli, create_root, register_source, write_workspace
from video_extract.learning import create_thread, record_feedback
from video_extract.learning_view import _validate_manifest_semantics, build_view, status_view
from video_extract.workspace import discover_workspace, rebuild


def test_generation_publishes_local_overview_and_search_catalog_together(tmp_path: Path) -> None:
    config, module_id, root_id, current_id = navigation_fixture(tmp_path)
    other = create_thread(config, module_id, "机器人动作怎样从识别结果产生？")["result"]["question"]

    built = build_view(config, module_id)

    assert built["status"] == "completed"
    generation = Path(built["result"]["generation_path"])
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert set(manifest["views"]) == {"局部问题图.html", "模块全景图.html", "问题目录.html"}
    for name, digest in manifest["views"].items():
        assert hashlib.sha256((generation / name).read_bytes()).hexdigest() == digest
    assert other["question_id"] in manifest["local_node_ids"]
    assert root_id not in manifest["local_node_ids"] and current_id not in manifest["local_node_ids"]
    assert len(manifest["root_groups"]) == 2
    overview = (generation / "模块全景图.html").read_text(encoding="utf-8")
    catalog = (generation / "问题目录.html").read_text(encoding="utf-8")
    assert "相机怎样标定？" in overview and "机器人动作怎样从识别结果产生？" in overview
    assert 'class="map-edge"' in overview
    assert 'id="query"' in catalog and "data-search=" in catalog
    assert built["result"]["generation_id"] in overview and manifest["learning_commit_id"] in catalog
    planned = rebuild(config, False)
    assert planned["plan"]["learning_views"]["module_ids"] == [module_id]


def test_portable_workspace_rebuilds_views_without_legacy_playback_tree(tmp_path: Path) -> None:
    config, module_id, _, _ = navigation_fixture(tmp_path)

    rebuilt = rebuild(config, True)

    assert rebuilt["ok"] is True
    assert rebuilt["written"] is True
    assert rebuilt["legacy_media_rebuild"] == "not_applicable_for_workspace_v2"
    assert rebuilt["learning_views"][0]["result"]["generation_id"].startswith("view-generation-")
    assert rebuilt["source_snapshot"]["sources"] == 1
    assert not (config.sources / "playback").exists()
    assert status_view(config, module_id)["status"] == "completed"


def test_feedback_change_republishes_all_views_and_modified_overview_is_unsynced(tmp_path: Path) -> None:
    config, module_id, _, current_id = navigation_fixture(tmp_path)
    first = build_view(config, module_id)
    record_feedback(config, current_id, "confused", "需要再解释一次")
    second = build_view(config, module_id)
    assert second["result"]["generation_id"] != first["result"]["generation_id"]
    generation = Path(second["result"]["generation_path"])
    for name in ("局部问题图.html", "模块全景图.html", "问题目录.html"):
        text = (generation / name).read_text(encoding="utf-8")
        assert ("仍不理解" in text) if name == "局部问题图.html" else ("confused" in text)
    overview = generation / "模块全景图.html"
    overview.write_text(overview.read_text(encoding="utf-8") + "\n人工修改\n", encoding="utf-8")
    assert status_view(config, module_id)["result"]["sync_state"] == "unsynced"
    failed = build_view(config, module_id)
    assert failed["status"] == "recoverable_failure"
    assert "人工修改" in overview.read_text(encoding="utf-8")


def test_cross_root_reference_is_a_dashed_boundary_in_local_and_overview_views(tmp_path: Path) -> None:
    config_path = write_workspace(tmp_path / "workspace"); source = register_source(tmp_path, config_path)
    source_thread, source_question = create_root(tmp_path, config_path, source, "概念根")
    commit_root(tmp_path, config_path, source, source_question)
    target_thread, target_question = create_root(tmp_path, config_path, source, "项目根")
    review_path = reuse_review(tmp_path / "reuse.json", source_question)
    _, prepared = cli("explanation", "prepare", "--question-id", target_question,
                      "--profile", "linear_transform", "--reuse-review", review_path,
                      "--workspace", config_path, "--json")
    draft, evidence, teaching = _linear_inputs(tmp_path, source, prepared["result"]["required_marker"])
    draft.write_text(draft.read_text() + "\n" + json.loads(review_path.read_text())["local_context"] + "\n")
    assert cli("explanation", "commit", "--question-id", target_question, "--draft", draft,
               "--evidence", evidence, "--teaching-review", teaching, "--profile", "linear_transform",
               "--preparation-id", prepared["result"]["preparation_id"],
               "--workspace", config_path, "--json")[0] == 0
    config = discover_workspace(config_path)
    target_module_id = cli("learning", "thread", "show", target_thread,
                           "--workspace", config_path, "--json")[1]["result"]["thread"]["module_id"]

    built = build_view(config, target_module_id)
    generation = Path(built["result"]["generation_path"])
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    reference = next(edge for edge in manifest["edges"] if edge["cross_root"])
    assert reference["from_question_id"] == target_question
    assert reference["to_question_id"] == source_question
    assert source_question in manifest["local_node_ids"]
    assert 'stroke-dasharray="8 6"' in (generation / "局部问题图.html").read_text(encoding="utf-8")
    overview = (generation / "模块全景图.html").read_text(encoding="utf-8")
    assert "跨根引用边界" in overview and 'stroke-dasharray="8 6"' in overview
    assert source_thread != target_thread


def test_obsidian_plugin_switches_all_generation_pinned_views() -> None:
    plugin = Path(__file__).parents[1] / "integrations/obsidian-learning-map"
    main = (plugin / "main.js").read_text(encoding="utf-8")
    assert "局部问题图.html" in main
    assert "模块全景图.html" in main
    assert "问题目录.html" in main
    assert "manifest.views" in main
    assert "打开只读学习导航" in main
    assert "正在查看旧版本" in main and "setInterval" in main


def test_same_module_cross_root_reference_is_a_valid_manifest_relationship(tmp_path: Path) -> None:
    config, module_id, root_id, _ = navigation_fixture(tmp_path)
    other = create_thread(config, module_id, "另一个根问题")["result"]["question"]
    built = build_view(config, module_id)
    manifest = json.loads(Path(built["result"]["manifest_path"]).read_text(encoding="utf-8"))
    manifest["edges"].append({
        "from_question_id": other["question_id"],
        "to_question_id": root_id,
        "kind": "reference",
        "cross_root": True,
        "reference_id": "reference-same-module-two-roots",
    })

    _validate_manifest_semantics(manifest)
