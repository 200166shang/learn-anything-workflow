import json
import multiprocessing
import time
from pathlib import Path
from unittest.mock import patch

from video_extract.cli import parser
from video_extract.source_registry import register
from video_extract.workspace import discover_workspace


WORKSPACE = '''schema_version = 2
workspace_id = "11111111-1111-4111-8111-111111111111"
[paths]
project = "project"
results = "results"
sources = "sources"
derived = "derived"
local = "local"
'''


def invoke(capsys, *args):
    parsed = parser().parse_args([*args, "--json"])
    code = parsed.func(parsed)
    return code, json.loads(capsys.readouterr().out)


def _process_worker(request_path: str, counter_path: str, queue) -> None:
    import video_extract.media_operations as operations

    def fake(item, kinds, target, **_):
        with Path(counter_path).open("a", encoding="utf-8") as stream:
            stream.write(item["id"] + "\n")
        time.sleep(0.25)
        target.mkdir(parents=True, exist_ok=True)
        outputs = {}
        for kind in kinds:
            path = target / f"{kind}.fixture"; path.write_bytes(kind.encode()); outputs[kind] = path
        return outputs

    operations._materialize_item = fake
    queue.put(operations.ensure_request(json.loads(Path(request_path).read_text())))


def request_fixture(tmp_path: Path) -> tuple[Path, dict]:
    workspace = tmp_path / "workspace.toml"
    workspace.write_text(WORKSPACE, encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"items": [
        {"id": "chapter-1", "source": "https://example.test/1", "title": "One"},
        {"id": "chapter-2", "source": "https://example.test/2", "title": "Two"},
    ]}), encoding="utf-8")
    registered = register(discover_workspace(workspace), catalog)
    request = {
        "contract_version": 1,
        "workspace": str(workspace),
        "source_id": registered["result"]["source_id"],
        "source_version": registered["result"]["source_version"],
        "scope": ["chapter-2"],
        "media": ["audio", "subtitles"],
        "language": "en",
        "quality": "standard",
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    return path, request


def test_plan_limits_work_to_explicit_scope_and_never_requests_dubbing(tmp_path, capsys):
    request, _ = request_fixture(tmp_path)
    code, result = invoke(capsys, "plan", "--request", str(request))
    assert code == 0
    assert [item["item_id"] for item in result["result"]["items"]] == ["chapter-2"]
    assert result["result"]["effective_parameters"] == {
        "language": "en", "media": ["audio", "subtitles"], "quality": "standard"
    }
    assert "dub" not in json.dumps(result).lower()


def test_same_logical_request_reuses_completed_operation_after_workspace_move(tmp_path, capsys):
    request_path, request = request_fixture(tmp_path)
    calls = []

    def fake(item, kinds, target, **_):
        calls.append(item["id"])
        target.mkdir(parents=True, exist_ok=True)
        outputs = {}
        for kind in kinds:
            path = target / f"{kind}.fixture"
            path.write_bytes(f"{item['id']}:{kind}".encode())
            outputs[kind] = path
        return outputs

    with patch("video_extract.media_operations._materialize_item", side_effect=fake):
        first_code, first = invoke(capsys, "ensure", "--request", str(request_path))
        second_code, second = invoke(capsys, "capability", "run", "media.acquire", "--request", str(request_path))
    assert first_code == second_code == 0
    assert first["operation_id"] == second["operation_id"]
    assert calls == ["chapter-2"]
    assert second["result"]["reuse"] == "verified_operation"

    moved = tmp_path.parent / f"{tmp_path.name}-moved"
    tmp_path.rename(moved)
    moved_request = moved / "request.json"
    data = json.loads(moved_request.read_text())
    data["workspace"] = str(moved / "workspace.toml")
    moved_request.write_text(json.dumps(data))
    with patch("video_extract.media_operations._materialize_item", side_effect=AssertionError("must reuse")):
        code, relocated = invoke(capsys, "operation", "show", first["operation_id"], "--workspace", str(moved / "workspace.toml"))
    assert code == 0
    assert relocated["operation_id"] == first["operation_id"]
    assert relocated["status"] == "completed"


def test_missing_artifact_resumes_only_the_gap(tmp_path, capsys):
    request_path, _ = request_fixture(tmp_path)
    calls = []

    def fake(item, kinds, target, **_):
        calls.extend(kinds)
        target.mkdir(parents=True, exist_ok=True)
        result = {}
        for kind in kinds:
            path = target / f"{kind}.fixture"; path.write_bytes(kind.encode()); result[kind] = path
        return result

    with patch("video_extract.media_operations._materialize_item", side_effect=fake):
        _, first = invoke(capsys, "ensure", "--request", str(request_path))
    audio = next(Path(ref) for ref in first["artifact_refs"] if "audio" in Path(ref).name)
    audio.unlink()
    calls.clear()
    with patch("video_extract.media_operations._materialize_item", side_effect=fake):
        code, resumed = invoke(capsys, "operation", "resume", first["operation_id"], "--workspace", str(tmp_path / "workspace.toml"))
    assert code == 0
    assert calls == ["audio"]
    assert resumed["status"] == "completed"


def test_busy_returns_same_operation_without_starting_second_adapter(tmp_path, capsys):
    request_path, _ = request_fixture(tmp_path)
    from video_extract.media_operations import operation_identity, write_test_lease

    operation_id = operation_identity(json.loads(request_path.read_text()))
    write_test_lease(discover_workspace(tmp_path / "workspace.toml"), operation_id, json.loads(request_path.read_text()))
    with patch("video_extract.media_operations._materialize_item", side_effect=AssertionError("duplicate")):
        code, result = invoke(capsys, "ensure", "--request", str(request_path))
    assert code == 3
    assert result["status"] == "busy"
    assert result["operation_id"] == operation_id


def test_two_processes_share_one_operation_and_one_adapter_run(tmp_path):
    request_path, _ = request_fixture(tmp_path)
    counter = tmp_path / "adapter-calls.txt"
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    first = context.Process(target=_process_worker, args=(str(request_path), str(counter), queue))
    second = context.Process(target=_process_worker, args=(str(request_path), str(counter), queue))
    first.start(); time.sleep(0.05); second.start()
    results = [queue.get(timeout=5), queue.get(timeout=5)]
    first.join(5); second.join(5)
    assert {result["status"] for result in results} == {"busy", "completed"}
    assert len({result["operation_id"] for result in results}) == 1
    assert counter.read_text().splitlines() == ["chapter-2"]
