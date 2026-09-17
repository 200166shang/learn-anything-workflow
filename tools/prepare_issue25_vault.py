"""Create a disposable Obsidian Vault for issue #25 visual acceptance."""

from __future__ import annotations

import argparse
import json
import shutil
import uuid
from pathlib import Path

from video_extract.learning import (commit_explanation, create_module, create_thread, prepare_explanation,
                                    pursue, record_feedback, show_module)
from video_extract.learning_view import build_view
from video_extract.source_registry import register
from video_extract.workspace import WorkspaceConfig


def prepare(target: Path) -> dict:
    target = target.resolve()
    if target.exists() and any(target.iterdir()):
        raise SystemExit(f"目标目录不是空的，拒绝覆盖：{target}")
    target.mkdir(parents=True, exist_ok=True)
    vault = target / "obsidian-vault"
    config_path = target / "workspace.toml"
    config_path.write_text(f'''schema_version = 2
workspace_id = "{uuid.uuid4()}"
[paths]
project = "project"
results = "results"
sources = "sources"
derived = "obsidian-vault"
local = "local"
''', encoding="utf-8")
    config = WorkspaceConfig.load(config_path)

    source_path = target / "source.md"
    source_path.write_text(
        "# Camera\nCalibration, projection, and linear transforms.\n",
        encoding="utf-8",
    )
    source = register(config, source_path)["result"]
    module = create_module(
        config, "理解视觉几何", "标定到投影",
        source["source_id"], source["source_version"],
    )["result"]["module"]
    rooted = create_thread(config, module["module_id"], "相机怎样标定？")["result"]
    root_id = rooted["question"]["question_id"]
    thread_id = rooted["thread"]["thread_id"]
    pending = pursue(config, thread_id, root_id, "deepens", "三维点怎样投影到图像？")["result"]["question"]
    broken = pursue(config, thread_id, pending["question_id"], "deepens", "线性变换是什么意思？")["result"]["question"]
    record_feedback(
        config, broken["question_id"], "parked", "先暂放",
        "矩阵的列为什么表示基向量去向？",
    )

    source_ref = show_module(config, module["module_id"])["result"]["module"]["source_refs"][0]
    prepared = prepare_explanation(config, root_id, "linear_transform")["result"]
    root_section = prepared["section_id"]
    draft = target / "explanation.md"
    draft.write_text(f'''# 相机标定与线性变换
<!-- section-id: {root_section} -->
直觉与因果：标定是在寻找坐标关系，基向量去向决定任意线性组合的去向。

机制与例子：矩阵的列记录基向量的像；矩阵 [[2,0],[0,3]] 把 (1,2) 变为 (2,6)。

条件与边界：平移不是线性变换，需要仿射或齐次坐标；坐标依赖所选基。

这是完整讲解的后半部分，用于验证 Obsidian 精确 section 跳转。
''', encoding="utf-8")
    evidence = target / "evidence.json"
    evidence.write_text(json.dumps([{
        "source_id": source_ref["source_id"],
        "source_version": source_ref["source_version"],
        "locator": {"kind": "heading", "value": "Camera"},
        "claim_type": "course_fact",
        "claim": "验收来源连接标定、投影与线性变换",
    }], ensure_ascii=False), encoding="utf-8")
    review = target / "review.json"
    review.write_text(json.dumps({
        "intuition": True, "causality": True, "mechanism": True,
        "worked_example": True, "conditions": True, "source_alignment": True,
        "basis_coordinate_reasoning": True, "affine_boundary": True,
    }), encoding="utf-8")
    section_map = target / "section-map.json"
    section_map.write_text(json.dumps({root_id: [root_section]}), encoding="utf-8")
    metadata = target / "revision-metadata.json"
    metadata.write_text(json.dumps({
        "summary": "固定 F-navigation 验收讲解",
        "affected_question_ids": [root_id],
    }, ensure_ascii=False), encoding="utf-8")
    committed = commit_explanation(
        config, root_id, draft, evidence, review, "linear_transform",
        prepared["preparation_id"], section_map_path=section_map,
        revision_metadata_path=metadata,
    )
    if committed["status"] != "completed":
        raise SystemExit(json.dumps(committed, ensure_ascii=False, indent=2))

    built = build_view(config, module["module_id"])
    if built["status"] != "completed":
        raise SystemExit(json.dumps(built, ensure_ascii=False, indent=2))

    plugin_source = Path(__file__).parents[1] / "integrations" / "obsidian-learning-map"
    plugin_target = vault / ".obsidian" / "plugins" / "video-extract-learning-map"
    plugin_target.mkdir(parents=True, exist_ok=True)
    for name in ("manifest.json", "main.js", "styles.css"):
        shutil.copy2(plugin_source / name, plugin_target / name)
    (vault / ".obsidian" / "community-plugins.json").write_text(
        '["video-extract-learning-map"]\n', encoding="utf-8",
    )

    result = {
        "workspace": str(config_path), "vault": str(vault),
        "module_id": module["module_id"], "thread_id": thread_id,
        "available_question_id": root_id,
        "pending_question_id": pending["question_id"],
        "parked_pending_question_id": broken["question_id"],
        "available_section_id": root_section,
        "generation_id": built["result"]["generation_id"],
        "learning_commit_id": built["result"]["learning_commit_id"],
    }
    (target / "acceptance.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    print(json.dumps(prepare(parser.parse_args().target), ensure_ascii=False, indent=2))
