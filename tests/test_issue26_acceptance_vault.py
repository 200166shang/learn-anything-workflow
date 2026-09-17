"""Acceptance-fixture coverage for issue #26."""

import json
from pathlib import Path

from tools.prepare_issue26_vault import prepare


def test_fixture_contains_two_roots_and_versioned_three_view_generation(tmp_path: Path) -> None:
    result = prepare(tmp_path / "fixture")
    vault = Path(result["vault"])
    views = vault / "learning-views"
    current = json.loads((views / "current" / f'{result["module_id"]}.json').read_text(encoding="utf-8"))
    generation = views / "generations" / current["generation_id"]
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))

    assert result["previous_generation_id"] != result["generation_id"]
    assert len(manifest["root_groups"]) == 2
    assert set(manifest["views"]) == {"局部问题图.html", "模块全景图.html", "问题目录.html"}
    assert "机器人动作怎样从识别结果产生？" in (generation / "问题目录.html").read_text(encoding="utf-8")
    assert (views / "generations" / result["previous_generation_id"] / "manifest.json").is_file()
    assert (vault / ".obsidian" / "plugins" / "video-extract-learning-map" / "main.js").is_file()
