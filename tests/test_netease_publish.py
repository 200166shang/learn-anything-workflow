import contextlib
import hashlib
import io
import json
import pytest
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from video_extract.cli import main
from video_extract.netease_adapter import NcmCliAdapter
from video_extract.netease_publish import register_netease_adapter
from video_extract.workspace import discover_workspace


class FakeNetEase:
    adapter_identity = "fixture.netease/v1"

    def __init__(self, pages=None, *, upload_result=None, reconcile_result=None, status_result=None):
        self.pages = pages or [{"items": [], "next_cursor": None}]
        self.upload_result = upload_result or {"query_handle": "task-1", "state": "submitted"}
        self.status_result = status_result or {"state": "completed"}
        self.reconcile_result = reconcile_result or {"state": "unknown"}
        self.calls = []

    def list_page(self, cursor, limit):
        self.calls.append(("list", cursor, limit))
        return self.pages.pop(0)

    def upload(self, path, *, idempotency_token):
        self.calls.append(("upload", path.name, idempotency_token))
        if isinstance(self.upload_result, Exception):
            raise self.upload_result
        return self.upload_result

    def status(self, query_handle):
        self.calls.append(("status", query_handle))
        return self.status_result

    def reconcile(self, attempt):
        self.calls.append(("reconcile", attempt["query_handle"]))
        return self.reconcile_result


def invoke(*args):
    stream = io.StringIO()
    with patch("sys.argv", ["video-extract", *args, "--json"]), contextlib.redirect_stdout(stream):
        code = main()
    return code, json.loads(stream.getvalue())


def fixture(tmp_path: Path):
    for name in ("project", "media", "sources", "derived", "results", "local"):
        (tmp_path / name).mkdir()
    workspace = tmp_path / "workspace.toml"
    workspace.write_text('''schema_version = 2
workspace_id = "11111111-1111-4111-8111-111111111111"
[paths]
project = "project"
sources = "sources"
derived = "derived"
results = "results"
local = "local"
''')
    package = tmp_path / "media" / "fixture"
    output = package / "listening" / "zh-CN" / "podcast.zh-CN.mp3"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"verified mandarin audio")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    package.joinpath("manifest.json").write_text(json.dumps({
        "schema_version": 5, "identity": "source-1", "title": "A: reliable / title?",
        "provenance": {"title": {"reliable": True}},
    }))
    audio_operation_id = "operation-" + "a" * 24
    receipts = tmp_path / "results" / "operation-receipts"
    receipts.mkdir(parents=True)
    receipts.joinpath(f"{audio_operation_id}.json").write_text(json.dumps({
        "operation_id": audio_operation_id, "status": "completed",
        "artifact_facts": {"output_sha256": digest},
    }))
    request = tmp_path / "request.json"
    base = {"contract_version": 1, "workspace": str(workspace), "package": str(package),
            "audio_operation_id": audio_operation_id, "output_sha256": digest,
            "account_ref": "netease-account-main", "adapter": "fixture"}
    request.write_text(json.dumps(base))
    return workspace, package, output, request, base


def test_authorization_is_required_before_any_remote_query(tmp_path):
    workspace, _, _, request, _ = fixture(tmp_path)
    adapter = FakeNetEase()
    register_netease_adapter("fixture", adapter)

    code, result = invoke("capability", "run", "publish.netease", "--request", str(request))

    assert code == 3
    assert result["status"] == "awaiting_user"
    assert result["next_action"]["type"] == "user"
    assert adapter.calls == []
    _, shown = invoke("operation", "show", result["operation_id"], "--workspace", str(workspace))
    assert shown["status"] == "awaiting_user"


def test_paginated_exact_visible_match_is_reused_with_sanitized_receipt(tmp_path):
    _, package, output, request, base = fixture(tmp_path)
    base["authorization_ref"] = "approval-33"
    request.write_text(json.dumps(base))
    adapter = FakeNetEase(pages=[
        {"items": [{"id": "other", "filename": "other.mp3", "duration": 9, "visible": True}], "next_cursor": "page-2"},
        {"items": [{"id": "remote-1", "filename": "A_ reliable _ title_.mp3", "duration": 10, "visible": True,
                    "sha256": base["output_sha256"]}], "next_cursor": None},
    ])
    register_netease_adapter("fixture", adapter)

    code, result = invoke("capability", "run", "publish.netease", "--request", str(request))

    assert code == 0
    assert result["status"] == "completed"
    assert not any(call[0] == "upload" for call in adapter.calls)
    receipt = json.loads((package / "publishing" / "netease" / "receipt.json").read_text())
    assert receipt["remote_id"] == "remote-1"
    assert receipt["audio_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert "authorization_ref" not in receipt


def test_ambiguous_same_name_candidates_remain_uncertain_without_upload(tmp_path):
    _, _, _, request, base = fixture(tmp_path)
    base["authorization_ref"] = "approval-33"
    request.write_text(json.dumps(base))
    items = [{"id": value, "filename": "A_ reliable _ title_.mp3", "duration": 10, "visible": True}
             for value in ("remote-1", "remote-2")]
    adapter = FakeNetEase(pages=[{"items": items, "next_cursor": None}])
    register_netease_adapter("fixture", adapter)

    _, result = invoke("capability", "run", "publish.netease", "--request", str(request))

    assert result["status"] == "uncertain"
    assert result["next_action"]["type"] == "reconcile"
    assert not any(call[0] == "upload" for call in adapter.calls)


def test_unique_same_name_duration_candidate_is_not_remote_identity_proof(tmp_path):
    _, _, _, request, base = fixture(tmp_path)
    base["authorization_ref"] = "approval-33"
    request.write_text(json.dumps(base))
    adapter = FakeNetEase(pages=[{"items": [{"id": "remote-candidate",
                                                "filename": "A_ reliable _ title_.mp3",
                                                "duration": 10, "visible": True}], "next_cursor": None}])
    register_netease_adapter("fixture", adapter)

    _, result = invoke("capability", "run", "publish.netease", "--request", str(request))

    assert result["status"] == "uncertain"
    assert result["validation"]["remote_object_identity"] == "ambiguous"
    assert not any(call[0] == "upload" for call in adapter.calls)


def test_upload_is_only_complete_after_status_and_visible_identity_match(tmp_path):
    _, package, _, request, base = fixture(tmp_path)
    base["authorization_ref"] = "approval-33"
    request.write_text(json.dumps(base))
    adapter = FakeNetEase(pages=[{"items": [], "next_cursor": None}, {
        "items": [{"id": "remote-2", "filename": "A_ reliable _ title_.mp3", "duration": 11,
                   "visible": True, "sha256": base["output_sha256"]}], "next_cursor": None}])
    register_netease_adapter("fixture", adapter)

    code, result = invoke("capability", "run", "publish.netease", "--request", str(request))

    assert code == 0
    assert result["status"] == "completed"
    assert [call[0] for call in adapter.calls] == ["list", "upload", "status", "list"]
    assert json.loads((package / "publishing" / "netease" / "receipt.json").read_text())["remote_id"] == "remote-2"


def test_stale_upload_copy_is_atomically_replaced_before_submission(tmp_path):
    _, package, output, request, base = fixture(tmp_path)
    base["authorization_ref"] = "approval-33"
    request.write_text(json.dumps(base))
    stale = package / "publishing" / "netease" / "A_ reliable _ title_.mp3"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale previous content")
    adapter = FakeNetEase(pages=[{"items": [], "next_cursor": None}, {
        "items": [{"id": "remote-new", "filename": stale.name, "sha256": base["output_sha256"],
                   "visible": True}], "next_cursor": None}])
    register_netease_adapter("fixture", adapter)

    _, result = invoke("capability", "run", "publish.netease", "--request", str(request))

    assert result["status"] == "completed"
    assert stale.read_bytes() == output.read_bytes()


def test_unknown_upload_outcome_only_reconciles_and_checks_remote_identity(tmp_path):
    workspace, _, _, request, base = fixture(tmp_path)
    base["authorization_ref"] = "approval-33"
    request.write_text(json.dumps(base))
    adapter = FakeNetEase(pages=[{"items": [], "next_cursor": None}],
                          upload_result=RuntimeError("connection lost"),
                          reconcile_result={"state": "available", "query_handle": "task-1",
                                            "remote_id": "wrong", "filename": "A_ reliable _ title_.mp3",
                                            "sha256": "0" * 64, "visible": True})
    register_netease_adapter("fixture", adapter)
    _, failed = invoke("capability", "run", "publish.netease", "--request", str(request))

    _, resumed = invoke("operation", "resume", failed["operation_id"], "--workspace", str(workspace))
    _, reconciled = invoke("operation", "reconcile", failed["operation_id"], "--workspace", str(workspace))

    assert failed["status"] == resumed["status"] == reconciled["status"] == "uncertain"
    assert [call[0] for call in adapter.calls].count("upload") == 1
    assert adapter.calls[-1][0] == "reconcile"
    assert reconciled["validation"]["remote_object_identity"] == "failed"


def test_authorization_can_be_added_without_changing_operation_identity(tmp_path):
    _, _, _, request, base = fixture(tmp_path)
    adapter = FakeNetEase(pages=[{"items": [], "next_cursor": None}])
    register_netease_adapter("fixture", adapter)
    _, waiting = invoke("capability", "run", "publish.netease", "--request", str(request))
    base["authorization_ref"] = "approval-later"
    request.write_text(json.dumps(base))
    _, continued = invoke("capability", "run", "publish.netease", "--request", str(request))

    assert waiting["operation_id"] == continued["operation_id"]
    assert continued["status"] == "uncertain"
    assert [call[0] for call in adapter.calls].count("upload") == 1


def test_reconcile_completes_only_with_matching_remote_receipt_identity(tmp_path):
    workspace, package, _, request, base = fixture(tmp_path)
    base["authorization_ref"] = "approval-33"
    request.write_text(json.dumps(base))
    adapter = FakeNetEase(pages=[{"items": [], "next_cursor": None}],
                          upload_result=RuntimeError("connection lost"),
                          reconcile_result={"state": "available", "query_handle": "task-1",
                                            "remote_id": "remote-3", "filename": "A_ reliable _ title_.mp3",
                                            "sha256": base["output_sha256"], "visible": True})
    register_netease_adapter("fixture", adapter)
    _, failed = invoke("capability", "run", "publish.netease", "--request", str(request))

    code, reconciled = invoke("operation", "reconcile", failed["operation_id"], "--workspace", str(workspace))

    assert code == 0
    assert reconciled["status"] == "completed"
    receipt = json.loads((package / "publishing" / "netease" / "receipt.json").read_text())
    assert receipt["remote_id"] == "remote-3"
    assert "approval-33" not in json.dumps(receipt)
    result_receipt = json.loads((workspace.parent / "results" / "operation-receipts" /
                                 f'{failed["operation_id"]}.json').read_text())
    assert result_receipt == receipt
    assert receipt["authoritative_revision"] >= 2
    assert len(receipt["authoritative_digest"]) == 64
    assert len(receipt["projection_digest"]) == 64


def test_show_is_read_only_and_reconcile_repairs_interrupted_receipt_projection(tmp_path):
    workspace, package, _, request, base = fixture(tmp_path)
    base["authorization_ref"] = "approval-33"
    request.write_text(json.dumps(base))
    adapter = FakeNetEase(pages=[{"items": [{"id": "remote-4", "filename": "A_ reliable _ title_.mp3",
                                               "sha256": base["output_sha256"], "visible": True}],
                                   "next_cursor": None}])
    register_netease_adapter("fixture", adapter)
    from video_extract import netease_publish
    original = netease_publish._project_receipts
    with patch("video_extract.netease_publish._project_receipts", side_effect=OSError("injected fsync interruption")):
        _, completed = invoke("capability", "run", "publish.netease", "--request", str(request))

    assert completed["status"] == "recoverable_failure"
    assert not (package / "publishing" / "netease" / "receipt.json").exists()
    authority = workspace.parent / "local" / "operations" / f'{completed["operation_id"]}.json'
    before = authority.read_bytes()
    _, shown = invoke("operation", "show", completed["operation_id"], "--workspace", str(workspace))
    assert shown["status"] == "recoverable_failure"
    assert authority.read_bytes() == before
    assert not (package / "publishing" / "netease" / "receipt.json").exists()
    with patch("video_extract.netease_publish._project_receipts", wraps=original):
        _, repaired = invoke("operation", "reconcile", completed["operation_id"], "--workspace", str(workspace))
    assert repaired["status"] == "completed"
    assert (package / "publishing" / "netease" / "receipt.json").is_file()


def test_unknown_background_status_cannot_be_completed_by_visible_candidate(tmp_path):
    _, _, _, request, base = fixture(tmp_path)
    base["authorization_ref"] = "approval-33"
    request.write_text(json.dumps(base))
    adapter = FakeNetEase(pages=[{"items": [], "next_cursor": None}, {
        "items": [{"id": "remote-5", "filename": "A_ reliable _ title_.mp3",
                   "sha256": base["output_sha256"], "visible": True}], "next_cursor": None}],
        status_result={"state": "unknown"})
    register_netease_adapter("fixture", adapter)

    _, result = invoke("capability", "run", "publish.netease", "--request", str(request))

    assert result["status"] == "uncertain"
    assert result["validation"]["remote_object_identity"] == "not_confirmed"


@pytest.mark.parametrize("remote_state", ["running", "failed"])
def test_noncompleted_background_status_safely_pauses_public_operation(tmp_path, remote_state):
    _, _, _, request, base = fixture(tmp_path)
    base["authorization_ref"] = "approval-33"
    request.write_text(json.dumps(base))
    register_netease_adapter("fixture", FakeNetEase(pages=[{"items": []}, {"items": [{
        "id": "remote-status", "filename": "A_ reliable _ title_.mp3",
        "sha256": base["output_sha256"], "visible": True}]}], status_result={"state": remote_state}))

    _, result = invoke("capability", "run", "publish.netease", "--request", str(request))

    assert result["status"] == "uncertain"
    assert result["next_action"]["type"] == "reconcile"


def test_unreliable_title_requests_user_input_before_remote_calls(tmp_path):
    _, package, _, request, _ = fixture(tmp_path)
    manifest = json.loads((package / "manifest.json").read_text())
    manifest["provenance"]["title"]["reliable"] = False
    (package / "manifest.json").write_text(json.dumps(manifest))
    adapter = FakeNetEase()
    register_netease_adapter("fixture", adapter)

    _, result = invoke("capability", "run", "publish.netease", "--request", str(request))

    assert result["status"] == "awaiting_user"
    assert result["validation"]["source_title"] == "failed"
    assert adapter.calls == []


def test_real_adapter_checks_installed_public_help_before_building_commands(tmp_path):
    audio = tmp_path / "fixture.mp3"
    audio.write_bytes(b"audio")
    outputs = iter([
        (0, "--cursor --limit --output", ""), (0, '{"items": [], "next_cursor": null}', ""),
        (0, "--background", ""), (0, "task-123", ""),
        (0, "taskId", ""), (0, '{"status":"completed"}', ""),
    ])
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        code, stdout, stderr = next(outputs)
        return type("Completed", (), {"returncode": code, "stdout": stdout, "stderr": stderr})()

    adapter = NcmCliAdapter()
    with patch("video_extract.netease_adapter.subprocess.run", side_effect=run):
        adapter.list_page(None, 30)
        uploaded = adapter.upload(audio, idempotency_token="token")
        adapter.status(uploaded["query_handle"])

    assert commands[0] == ["ncm-cli", "cloud", "list", "--help"]
    assert commands[2] == ["ncm-cli", "cloudupload", "upload", "--help"]
    assert commands[4] == ["ncm-cli", "cloudupload", "status", "--help"]
    assert all("--userInput" not in command for command in commands)


def test_real_adapter_exit_zero_parses_running_failed_and_completed_states():
    adapter = NcmCliAdapter()
    for raw, expected in [('{"status":"running"}', "running"), ('{"data":{"state":"failed"}}', "failed"),
                          ('{"status":"completed"}', "completed"), ('{"status":"mystery"}', "unknown")]:
        outputs = iter([(0, "taskId", ""), (0, raw, "")])
        with patch("video_extract.netease_adapter.subprocess.run",
                   side_effect=lambda *a, **k: type("Completed", (), dict(zip(
                       ("returncode", "stdout", "stderr"), next(outputs))))()):
            assert adapter.status("task-1")["state"] == expected


def test_tampered_receipt_is_read_only_on_show_and_repaired_by_reconcile(tmp_path):
    workspace, package, _, request, base = fixture(tmp_path)
    base["authorization_ref"] = "approval-33"
    request.write_text(json.dumps(base))
    adapter = FakeNetEase(pages=[{"items": [{"id": "remote-6", "filename": "A_ reliable _ title_.mp3",
                                               "sha256": base["output_sha256"], "visible": True}]}])
    register_netease_adapter("fixture", adapter)
    _, completed = invoke("capability", "run", "publish.netease", "--request", str(request))
    receipt_path = package / "publishing" / "netease" / "receipt.json"
    damaged = json.loads(receipt_path.read_text())
    damaged["projection_digest"] = "0" * 64
    receipt_path.write_text(json.dumps(damaged))
    authority = workspace.parent / "local" / "operations" / f'{completed["operation_id"]}.json'
    before = authority.read_bytes()

    _, shown = invoke("operation", "show", completed["operation_id"], "--workspace", str(workspace))
    assert shown["status"] == "recoverable_failure"
    assert authority.read_bytes() == before
    assert json.loads(receipt_path.read_text())["projection_digest"] == "0" * 64
    _, repaired = invoke("operation", "reconcile", completed["operation_id"], "--workspace", str(workspace))
    assert repaired["status"] == "completed"
    assert json.loads(receipt_path.read_text())["projection_digest"] != "0" * 64


def test_tampered_authority_cannot_reproject_receipt(tmp_path):
    workspace, package, _, request, base = fixture(tmp_path)
    base["authorization_ref"] = "approval-33"
    request.write_text(json.dumps(base))
    register_netease_adapter("fixture", FakeNetEase(pages=[{"items": [{"id": "remote-7",
        "filename": "A_ reliable _ title_.mp3", "sha256": base["output_sha256"], "visible": True}]}]))
    _, completed = invoke("capability", "run", "publish.netease", "--request", str(request))
    authority = workspace.parent / "local" / "operations" / f'{completed["operation_id"]}.json'
    record = json.loads(authority.read_text())
    record["remote"]["id"] = "tampered"
    authority.write_text(json.dumps(record))
    receipt_path = package / "publishing" / "netease" / "receipt.json"
    before = receipt_path.read_bytes()

    _, shown = invoke("operation", "show", completed["operation_id"], "--workspace", str(workspace))
    _, result = invoke("operation", "reconcile", completed["operation_id"], "--workspace", str(workspace))

    assert shown["status"] == "uncertain"
    assert shown["validation"]["authoritative_record"] == "failed"
    assert result["status"] == "uncertain"
    assert result["validation"]["authoritative_record"] == "failed"
    assert receipt_path.read_bytes() == before


def test_concurrent_reconcile_cannot_overwrite_completed_winner(tmp_path):
    workspace_path, package, _, request, base = fixture(tmp_path)
    base["authorization_ref"] = "approval-33"
    request.write_text(json.dumps(base))
    entered = threading.Event()
    release = threading.Event()

    class BarrierAdapter(FakeNetEase):
        def reconcile(self, attempt):
            self.calls.append(("reconcile", attempt["query_handle"]))
            entered.set()
            assert release.wait(5)
            return {"state": "available", "query_handle": attempt["query_handle"],
                    "remote_id": "remote-winner", "filename": "A_ reliable _ title_.mp3",
                    "sha256": base["output_sha256"], "visible": True}

    adapter = BarrierAdapter(pages=[{"items": []}], upload_result=RuntimeError("lost response"))
    register_netease_adapter("fixture", adapter)
    _, failed = invoke("capability", "run", "publish.netease", "--request", str(request))
    config = discover_workspace(workspace_path)
    from video_extract.netease_publish import reconcile_operation, show_operation

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(reconcile_operation, config, failed["operation_id"])
        assert entered.wait(5)
        concurrent = pool.submit(reconcile_operation, config, failed["operation_id"]).result(timeout=5)
        release.set()
        completed = winner.result(timeout=5)

    final = show_operation(config, failed["operation_id"])
    assert concurrent["status"] == "busy"
    assert completed["status"] == final["status"] == "completed"
    assert [call[0] for call in adapter.calls].count("reconcile") == 1
    receipts = [json.loads((package / "publishing" / "netease" / "receipt.json").read_text()),
                json.loads((workspace_path.parent / "results" / "operation-receipts" /
                            f'{failed["operation_id"]}.json').read_text())]
    assert receipts[0] == receipts[1]
    assert receipts[0]["remote_id"] == "remote-winner"
