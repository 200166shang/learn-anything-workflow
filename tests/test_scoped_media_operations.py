import json
import multiprocessing
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


class FixtureAdapter:
    def __init__(self, *, fail=False, reconcile_state="retry_safe", counter=None, delay=0, on_acquire=None):
        self.fail = fail
        self.reconcile_state = reconcile_state
        self.counter = counter if counter is not None else []
        self.delay = delay
        self.on_acquire = on_acquire
        self.tokens = []
        self.kinds = []

    def acquire(self, item, kinds, target, *, language, quality, idempotency_token):
        self.counter.append(item["id"])
        self.tokens.append(idempotency_token)
        self.kinds.append(list(kinds))
        if self.on_acquire:
            self.on_acquire()
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("connection lost")
        target.mkdir(parents=True, exist_ok=True)
        outputs = {}
        for kind in kinds:
            path = target / f"{kind}.fixture"; path.write_bytes(f"{item['id']}:{kind}".encode()); outputs[kind] = path
        return {"artifacts": outputs, "query_handle": idempotency_token}

    def reconcile(self, attempt, target):
        result = {"state": self.reconcile_state, "query_handle": attempt["query_handle"], "evidence": "fixture ledger"}
        if self.reconcile_state == "available":
            result["artifacts"] = {kind: target / f"{kind}.fixture" for kind in attempt["media"]}
        return result


def use_adapter(adapter):
    from video_extract.media_operations import register_media_adapter
    register_media_adapter("fixture.media-v1", adapter)
    return adapter


def invoke(capsys, *args):
    parsed = parser().parse_args([*args, "--json"])
    code = parsed.func(parsed)
    return code, json.loads(capsys.readouterr().out)


def _process_worker(request_path: str, counter_path: str, queue) -> None:
    import video_extract.media_operations as operations
    class ProcessAdapter(FixtureAdapter):
        def acquire(self, item, kinds, target, **kwargs):
            with Path(counter_path).open("a", encoding="utf-8") as stream:
                stream.write(item["id"] + "\n")
            return super().acquire(item, kinds, target, **kwargs)
    operations.register_media_adapter("fixture.media-v1", ProcessAdapter(delay=0.25))
    queue.put(operations.ensure_request(json.loads(Path(request_path).read_text())))


def request_fixture(tmp_path: Path) -> tuple[Path, dict]:
    workspace = tmp_path / "workspace.toml"
    workspace.write_text(WORKSPACE, encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"items": [
        {"id": "chapter-1", "source": "https://example.test/1", "title": "One", "adapter": "fixture.media-v1"},
        {"id": "chapter-2", "source": "https://example.test/2", "title": "Two", "adapter": "fixture.media-v1"},
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
    adapter = use_adapter(FixtureAdapter())
    first_code, first = invoke(capsys, "ensure", "--request", str(request_path))
    second_code, second = invoke(capsys, "capability", "run", "media.acquire", "--request", str(request_path))
    assert first_code == second_code == 0
    assert first["operation_id"] == second["operation_id"]
    assert adapter.counter == ["chapter-2"]
    assert adapter.kinds[-1] == ["audio", "subtitles"]
    assert second["result"]["reuse"] == "verified_operation"

    moved = tmp_path.parent / f"{tmp_path.name}-moved"
    tmp_path.rename(moved)
    moved_request = moved / "request.json"
    data = json.loads(moved_request.read_text())
    data["workspace"] = str(moved / "workspace.toml")
    moved_request.write_text(json.dumps(data))
    code, relocated = invoke(capsys, "operation", "show", first["operation_id"], "--workspace", str(moved / "workspace.toml"))
    assert code == 0
    assert relocated["operation_id"] == first["operation_id"]
    assert relocated["status"] == "completed"


def test_missing_artifact_resumes_only_the_gap(tmp_path, capsys):
    request_path, _ = request_fixture(tmp_path)
    adapter = use_adapter(FixtureAdapter())
    _, first = invoke(capsys, "ensure", "--request", str(request_path))
    audio = next(Path(ref) for ref in first["artifact_refs"] if "audio" in Path(ref).name)
    audio.unlink()
    adapter.counter.clear()
    code, resumed = invoke(capsys, "operation", "resume", first["operation_id"], "--workspace", str(tmp_path / "workspace.toml"))
    assert code == 0
    assert adapter.counter == ["chapter-2"]
    assert adapter.kinds[-1] == ["audio"]
    assert resumed["status"] == "completed"


def test_busy_returns_same_operation_without_starting_second_adapter(tmp_path, capsys):
    request_path, _ = request_fixture(tmp_path)
    from video_extract.media_operations import operation_identity, write_test_lease

    operation_id = operation_identity(json.loads(request_path.read_text()))
    write_test_lease(discover_workspace(tmp_path / "workspace.toml"), operation_id, json.loads(request_path.read_text()))
    adapter = use_adapter(FixtureAdapter())
    code, result = invoke(capsys, "ensure", "--request", str(request_path))
    assert code == 3
    assert result["status"] == "busy"
    assert result["operation_id"] == operation_id
    assert adapter.counter == []


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


def test_adapter_failure_is_uncertain_until_reconciled_as_not_submitted(tmp_path, capsys):
    request_path, _ = request_fixture(tmp_path)
    adapter = use_adapter(FixtureAdapter(fail=True, reconcile_state="not_submitted"))
    code, failed = invoke(capsys, "ensure", "--request", str(request_path))
    assert code == 3
    assert failed["status"] == "uncertain"
    assert failed["next_action"]["type"] == "reconcile"

    code, reconciled = invoke(capsys, "operation", "reconcile", failed["operation_id"], "--workspace", str(tmp_path / "workspace.toml"))
    assert code == 1
    assert reconciled["status"] == "recoverable_failure"
    assert reconciled["next_action"]["type"] == "resume"
    assert adapter.counter == ["chapter-2"]


def test_uncertain_operation_cannot_resume_without_reconciliation(tmp_path, capsys):
    request_path, _ = request_fixture(tmp_path)
    adapter = use_adapter(FixtureAdapter(fail=True))
    _, failed = invoke(capsys, "ensure", "--request", str(request_path))
    code, result = invoke(capsys, "operation", "resume", failed["operation_id"], "--workspace", str(tmp_path / "workspace.toml"))
    assert code == 3
    assert result["status"] == "uncertain"
    assert adapter.counter == ["chapter-2"]


def test_committed_reconciliation_never_allows_resume_or_second_adapter_call(tmp_path, capsys):
    request_path, _ = request_fixture(tmp_path)
    adapter = use_adapter(FixtureAdapter(fail=True, reconcile_state="committed"))
    _, failed = invoke(capsys, "ensure", "--request", str(request_path))
    _, reconciled = invoke(capsys, "operation", "reconcile", failed["operation_id"],
                           "--workspace", str(tmp_path / "workspace.toml"))
    assert reconciled["status"] == "uncertain"
    assert reconciled["next_action"]["type"] == "reconcile"
    code, resumed = invoke(capsys, "operation", "resume", failed["operation_id"],
                           "--workspace", str(tmp_path / "workspace.toml"))
    assert code == 3
    assert resumed["status"] == "uncertain"
    assert adapter.counter == ["chapter-2"]


def test_unsupported_reconciliation_stays_uncertain_and_requires_user_action(tmp_path, capsys):
    request_path, _ = request_fixture(tmp_path)
    from video_extract.media_operations import MEDIA_ADAPTERS
    MEDIA_ADAPTERS.pop("fixture.media-v1", None)
    _, failed = invoke(capsys, "ensure", "--request", str(request_path))
    code, reconciled = invoke(capsys, "operation", "reconcile", failed["operation_id"],
                              "--workspace", str(tmp_path / "workspace.toml"))
    assert code == 3
    assert reconciled["status"] == "uncertain"
    assert reconciled["next_action"]["type"] == "user"


def test_adapter_protocol_recovers_available_result_without_reacquiring(tmp_path, capsys):
    request_path, _ = request_fixture(tmp_path)

    class CompletedThenDisconnected(FixtureAdapter):
        def acquire(self, *args, **kwargs):
            result = super().acquire(*args, **kwargs)
            raise RuntimeError("response lost after completion")

    adapter = use_adapter(CompletedThenDisconnected(reconcile_state="available"))
    _, failed = invoke(capsys, "ensure", "--request", str(request_path))
    code, reconciled = invoke(capsys, "operation", "reconcile", failed["operation_id"],
                              "--workspace", str(tmp_path / "workspace.toml"))
    assert code == 0
    assert reconciled["status"] == "completed"
    assert len(reconciled["artifact_refs"]) == 2
    assert adapter.counter == ["chapter-2"]


def test_reconcile_query_failure_is_v1_uncertain_then_available_without_reacquire(tmp_path, capsys):
    request_path, _ = request_fixture(tmp_path)

    class QueryFailsOnce(FixtureAdapter):
        def __init__(self):
            super().__init__(reconcile_state="available")
            self.queries = 0

        def acquire(self, *args, **kwargs):
            super().acquire(*args, **kwargs)
            raise RuntimeError("response lost after completion")

        def reconcile(self, attempt, target):
            self.queries += 1
            if self.queries == 1:
                raise RuntimeError("query transport unavailable")
            return super().reconcile(attempt, target)

    adapter = use_adapter(QueryFailsOnce())
    _, failed = invoke(capsys, "ensure", "--request", str(request_path))
    operation_path = tmp_path / "local" / "operations" / f"{failed['operation_id']}.json"
    before = json.loads(operation_path.read_text())

    code, query_failed = invoke(capsys, "operation", "reconcile", failed["operation_id"],
                                "--workspace", str(tmp_path / "workspace.toml"))
    after_failure = json.loads(operation_path.read_text())
    assert code == 3
    assert query_failed["api_version"] == 1
    assert query_failed["status"] == "uncertain"
    assert query_failed["next_action"]["type"] == "reconcile"
    assert "video-extract operation reconcile" in query_failed["next_action"]["command"]
    assert "query transport unavailable" in query_failed["diagnostics"][0]
    assert after_failure["current_attempt"] == before["current_attempt"]
    assert after_failure["reconciliation"][-1]["state"] == "query_failed"

    code, completed = invoke(capsys, "operation", "reconcile", failed["operation_id"],
                             "--workspace", str(tmp_path / "workspace.toml"))
    assert code == 0
    assert completed["status"] == "completed"
    assert adapter.counter == ["chapter-2"]
    assert adapter.queries == 2


def test_idempotency_token_is_stable_across_reconciled_resume(tmp_path, capsys):
    request_path, _ = request_fixture(tmp_path)
    adapter = use_adapter(FixtureAdapter(fail=True, reconcile_state="retry_safe"))
    _, failed = invoke(capsys, "ensure", "--request", str(request_path))
    _, reconciled = invoke(capsys, "operation", "reconcile", failed["operation_id"],
                           "--workspace", str(tmp_path / "workspace.toml"))
    adapter.fail = False
    code, completed = invoke(capsys, "operation", "resume", failed["operation_id"],
                             "--workspace", str(tmp_path / "workspace.toml"))
    assert code == 0
    assert completed["status"] == "completed"
    assert len(adapter.tokens) == 2
    assert adapter.tokens[0] == adapter.tokens[1]


def test_stale_fence_adapter_result_is_rejected_before_operation_commit(tmp_path, capsys):
    request_path, request = request_fixture(tmp_path)
    from video_extract.manifest import atomic_write_json, read_json
    from video_extract.media_operations import operation_identity
    operation_id = operation_identity(request)
    operation_path = tmp_path / "local" / "operations" / f"{operation_id}.json"

    def steal_fence():
        record = read_json(operation_path)
        record["lease"]["owner"] = "new-owner"
        record["lease"]["fencing"] += 1
        atomic_write_json(operation_path, record)

    use_adapter(FixtureAdapter(on_acquire=steal_fence))
    code, result = invoke(capsys, "ensure", "--request", str(request_path))
    persisted = read_json(operation_path)
    assert code == 3
    assert result["status"] == "busy"
    assert persisted.get("artifacts") == {}
    assert persisted["lease"]["owner"] == "new-owner"


def test_stale_running_lease_becomes_uncertain_instead_of_permanent_busy(tmp_path, capsys):
    request_path, request = request_fixture(tmp_path)
    from video_extract.media_operations import operation_identity, write_test_lease
    workspace = discover_workspace(tmp_path / "workspace.toml")
    operation_id = operation_identity(request)
    write_test_lease(workspace, operation_id, request,
                     expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    code, result = invoke(capsys, "ensure", "--request", str(request_path))
    assert code == 3
    assert result["status"] == "uncertain"
    assert result["next_action"]["type"] == "reconcile"


def test_request_schema_rejects_wrong_version_and_extra_fields_for_every_entry(tmp_path, capsys):
    request_path, request = request_fixture(tmp_path)
    request["contract_version"] = 2
    request["secret"] = "must-not-be-accepted"
    request_path.write_text(json.dumps(request))
    for command in (("plan", "--request", str(request_path)),
                    ("ensure", "--request", str(request_path)),
                    ("capability", "run", "media.acquire", "--request", str(request_path))):
        code, result = invoke(capsys, *command)
        assert code != 0
        assert result["api_version"] == 1
        assert result["status"] in {"missing_input", "unsupported"}
        assert result["validation"]

    request["contract_version"] = 1
    request_path.write_text(json.dumps(request))
    for command in (("plan", "--request", str(request_path)),
                    ("ensure", "--request", str(request_path)),
                    ("capability", "run", "media.acquire", "--request", str(request_path))):
        code, result = invoke(capsys, *command)
        assert code != 0
        assert result["api_version"] == 1
        assert result["validation"]


def test_show_unknown_operation_does_not_create_operation_directory(tmp_path, capsys):
    request_fixture(tmp_path)
    operations = tmp_path / "local" / "operations"
    assert not operations.exists()
    code, result = invoke(capsys, "operation", "show", "operation-000000000000000000000000", "--workspace", str(tmp_path / "workspace.toml"))
    assert code == 3
    assert result["status"] == "missing_input"
    assert not operations.exists()
