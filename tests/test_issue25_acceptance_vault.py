import json
import subprocess
import sys
from pathlib import Path


def test_prepares_issue25_acceptance_vault(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    target = tmp_path / "acceptance"
    done = subprocess.run(
        [sys.executable, "tools/prepare_issue25_vault.py", str(target)],
        cwd=root, capture_output=True, text=True, check=True,
    )
    result = json.loads(done.stdout)
    vault = Path(result["vault"])
    assert (vault / ".obsidian/plugins/video-extract-learning-map/main.js").is_file()
    manifest = json.loads(next((vault / "learning-views/generations").glob(
        "*/manifest.json")).read_text(encoding="utf-8"))
    assert manifest["nodes"][result["available_question_id"]]["content_state"] == "available"
    assert manifest["nodes"][result["pending_question_id"]]["content_state"] == "pending"
    parked = manifest["nodes"][result["parked_pending_question_id"]]
    assert parked["content_state"] == "pending"
    assert parked["feedback"] == "parked"
