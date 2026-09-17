"""Public CLI evidence that adopted attachments survive publication and restore."""
import json
import re
from pathlib import Path

import pytest

from test_review_workflow import cli, workspace


def request_with_image(tmp_path, markdown):
    config = workspace(tmp_path)
    source = tmp_path / "source.md"
    source.write_text("# Fact\nEvidence.\n")
    code, registered = cli("source", "register", source, "--workspace", config.config_path, "--json")
    assert code == 0, registered
    image = tmp_path / "frame.png"
    image.write_bytes(b"real-image-fixture")
    value = {"schema_version": 1, "source_id": registered["result"]["source_id"],
             "source_version": registered["result"]["source_version"], "expected_revision": 0,
             "markdown": markdown, "citations": [{"claim": "Fact", "locator_type": "heading", "locator": "# Fact"}],
             "corrections": [], "attachments": [str(image)], "association": {"status": "not_applicable"}}
    request = tmp_path / "finalize.json"
    request.write_text(json.dumps(value))
    return config, request, image


def test_published_attachment_link_survives_source_removal_and_backup_restore(tmp_path):
    config, request, image = request_with_image(tmp_path, "# Fact\n![Evidence](frame.png)\n")
    code, finalized = cli("notes", "finalize", json.loads(request.read_text())["source_id"], "--request", request, "--workspace", config.config_path, "--json")
    assert code == 0, finalized
    note = Path(finalized["result"]["note"])
    destination = re.search(r"!\[Evidence\]\(([^)]+)\)", note.read_text()).group(1)
    image.unlink()
    assert (note.parent / destination).read_bytes() == b"real-image-fixture"
    backup = tmp_path / "backup"
    assert cli("backup", "create", backup, "--workspace", config.config_path, "--json")[0] == 0
    relative_note = note.relative_to(config.results)
    config.results.rename(tmp_path / "original-results-unavailable")
    target = workspace(tmp_path / "restored")
    code, restored = cli("backup", "restore", backup, "--workspace", target.config_path, "--json")
    assert code == 0, restored
    restored_note = target.results / relative_note
    assert (restored_note.parent / destination).read_bytes() == b"real-image-fixture"


@pytest.mark.parametrize("markdown", ["Only mention frame.png", "`![Evidence](frame.png)`", "<!-- ![Evidence](frame.png) -->", "\\[Evidence](frame.png)", "```md\n![Evidence](frame.png)\n```", "```md\n![Evidence](frame.png)"])
def test_filename_mentions_and_nonrendered_images_are_not_adoption(tmp_path, markdown):
    config, request, _ = request_with_image(tmp_path, markdown)
    code, rejected = cli("notes", "finalize", json.loads(request.read_text())["source_id"], "--request", request, "--workspace", config.config_path, "--json")
    assert code != 0
    assert rejected["status"] != "completed"


def test_inline_attachment_titles_and_fences_preserve_valid_links_and_replay(tmp_path):
    config, request, _ = request_with_image(
        tmp_path, '# Fact\n```md\nexample only\n````\n[Evidence](<frame.png> "figure title")\n')
    source_id = json.loads(request.read_text())["source_id"]
    args = ("notes", "finalize", source_id, "--request", request, "--workspace", config.config_path, "--json")
    code, first = cli(*args)
    assert code == 0, first
    value = json.loads(request.read_text())
    value["expected_revision"] = 1
    request.write_text(json.dumps(value))
    code, replayed = cli(*args)
    assert code == 0, replayed
    assert replayed["result"]["action"] == "reused"
    assert replayed["result"]["note"] == first["result"]["note"]
    assert '"figure title"' in Path(first["result"]["note"]).read_text()
