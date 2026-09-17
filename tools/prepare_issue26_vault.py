"""Create a disposable two-root Obsidian Vault for issue #26 acceptance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.prepare_issue25_vault import prepare as prepare_issue25
from video_extract.learning import create_thread, pursue, record_feedback
from video_extract.learning_view import build_view
from video_extract.workspace import WorkspaceConfig


def prepare(target: Path) -> dict:
    result = prepare_issue25(target)
    config = WorkspaceConfig.load(Path(result["workspace"]))

    second = create_thread(
        config,
        result["module_id"],
        "机器人动作怎样从识别结果产生？",
    )["result"]
    second_root_id = second["question"]["question_id"]
    second_child = pursue(
        config,
        second["thread"]["thread_id"],
        second_root_id,
        "deepens",
        "坐标变换怎样影响动作规划？",
    )["result"]["question"]
    record_feedback(
        config,
        second_child["question_id"],
        "confused",
        "需要结合机器人坐标系继续解释",
    )

    built = build_view(config, result["module_id"])
    if built["status"] != "completed":
        raise SystemExit(json.dumps(built, ensure_ascii=False, indent=2))

    result.update({
        "second_thread_id": second["thread"]["thread_id"],
        "second_root_question_id": second_root_id,
        "second_pending_question_id": second_child["question_id"],
        "previous_generation_id": result["generation_id"],
        "generation_id": built["result"]["generation_id"],
        "learning_commit_id": built["result"]["learning_commit_id"],
    })
    (target.resolve() / "acceptance.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    print(json.dumps(prepare(parser.parse_args().target), ensure_ascii=False, indent=2))
