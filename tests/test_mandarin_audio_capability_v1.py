import json
import subprocess
from pathlib import Path

from video_extract.cli import parser
from video_extract.mandarin_audio import MandarinAdapter, register_mandarin_adapter


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


def fixture(tmp_path: Path, language: str) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "workspace.toml"
    workspace.write_text(WORKSPACE, encoding="utf-8")
    package = tmp_path / "package"
    audio = package / "media/audio.source.m4a"
    audio.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.25", "-c:a", "aac", str(audio)], check=True)
    manifest = {"schema_version": 5, "identity": "fixture-audio", "artifacts": {"source_audio": "media/audio.source.m4a"},
                "provenance": {"source_audio": {"language": language, "kind": "native_chinese_track" if language == "zh-CN" else "source_track"}}}
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"contract_version": 1, "workspace": str(workspace), "package": str(package),
                                   "source_version": "source-version-1", "profile": "alibaba-podcast-tts-throughput"}), encoding="utf-8")
    return workspace, package, request


class FakePaidAdapter(MandarinAdapter):
    adapter_identity = "fixture.mandarin-paid-v1"
    authorization_category = "paid_tts"
    requires_authorization = True

    def __init__(self):
        self.calls = []

    def localize(self, source, target, *, profile, idempotency_token):
        self.calls.append(idempotency_token)
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                        "-ar", "48000", "-ac", "1", "-b:a", "64k", str(target)], check=True)
        return {"query_handle": idempotency_token, "artifact": target,
                "receipt": {"receipt_id": "fixture-receipt"}}

    def reconcile(self, attempt, target):
        return {"state": "available", "query_handle": attempt["query_handle"], "artifact": target,
                "receipt": {"receipt_id": "fixture-receipt"}}


class InterruptedAdapter(FakePaidAdapter):
    adapter_identity = "fixture.mandarin-interrupted-v1"

    def __init__(self):
        super().__init__()
        self.interrupted = True

    def localize(self, source, target, *, profile, idempotency_token):
        self.calls.append(idempotency_token)
        if self.interrupted:
            self.interrupted = False
            raise RuntimeError("connection lost after paid submission")
        return super().localize(source, target, profile=profile, idempotency_token=idempotency_token)

    def reconcile(self, attempt, target):
        return {"state": "committed", "query_handle": attempt["query_handle"],
                "receipt": {"receipt_id": "remote-pending"}}


class RecoverableInterruptedAdapter(InterruptedAdapter):
    adapter_identity = "fixture.mandarin-recoverable-v1"

    def reconcile(self, attempt, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        source = target.parents[2] / "media/audio.source.m4a"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                        "-ar", "48000", "-ac", "1", "-b:a", "64k", str(target)], check=True)
        return {"state": "available", "query_handle": attempt["query_handle"], "artifact": target,
                "receipt": {"receipt_id": "recovered-receipt"}}


def test_native_chinese_normalizes_locally_and_reuses_only_verified_fingerprint(tmp_path, capsys):
    workspace, package, request = fixture(tmp_path, "zh-CN")
    adapter = FakePaidAdapter()
    register_mandarin_adapter("fixture-paid", adapter)

    code, first = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    code2, reused = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))

    assert code == code2 == 0
    assert adapter.calls == []
    assert reused["result"]["reuse"] == "verified_operation"
    assert reused["validation"] == {"source": "passed", "audio_spec": "passed", "output_digest": "passed"}
    output = package / "listening/zh-CN/podcast.zh-CN.mp3"
    assert output.is_file()
    report = json.loads((output.parent / "production-report.json").read_text())
    assert report["mode"] == "native"
    assert "authorization_ref" not in report

    # A format-valid replacement source must not cause reuse of old content.
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=880:duration=0.25", "-c:a", "aac", str(package / "media/audio.source.m4a")], check=True)
    changed_code, changed = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert changed_code == 3
    assert changed["status"] == "missing_input"
    assert changed["validation"]["source"] == "failed"


def test_english_requires_authorization_then_uses_fake_paid_adapter(tmp_path, capsys):
    workspace, package, request = fixture(tmp_path, "en")
    adapter = FakePaidAdapter()
    register_mandarin_adapter("fixture-paid", adapter)
    data = json.loads(request.read_text())
    data["adapter"] = "fixture-paid"
    request.write_text(json.dumps(data))

    code, waiting = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert code == 3
    assert waiting["status"] == "awaiting_user"
    assert adapter.calls == []

    data["authorization_ref"] = "approval-ticket-42"
    request.write_text(json.dumps(data))
    complete_code, complete = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert complete_code == 0
    assert len(adapter.calls) == 1
    assert complete["status"] == "completed"
    assert complete["operation_id"] == waiting["operation_id"]
    receipt = json.loads((tmp_path / "results/operation-receipts" / f'{complete["operation_id"]}.json').read_text())
    assert receipt["intent"]["authorization_ref"] == "approval-ticket-42"
    assert receipt["artifact_facts"]["output"]["validation"] == "ffprobe-48khz-mono-approx64kbps"


def test_unknown_language_is_rejected_before_adapter_call(tmp_path, capsys):
    _, _, request = fixture(tmp_path, "unknown")
    adapter = FakePaidAdapter()
    register_mandarin_adapter("fixture-paid", adapter)
    data = json.loads(request.read_text()); data["adapter"] = "fixture-paid"; data["authorization_ref"] = "approved"
    request.write_text(json.dumps(data))

    code, result = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))

    assert code == 1
    assert result["status"] == "unsupported"
    assert adapter.calls == []
    assert result["validation"]["source_language"] == "failed"


def test_parameter_change_has_distinct_operation_and_does_not_reuse_old_mp3(tmp_path, capsys):
    _, _, request = fixture(tmp_path, "zh-CN")
    _, first = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    data = json.loads(request.read_text()); data["bitrate_kbps"] = 72
    request.write_text(json.dumps(data))

    _, changed = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))

    assert changed["operation_id"] != first["operation_id"]
    assert changed["result"]["reuse"] is None


def test_uncertain_paid_submission_is_shown_and_resumed_without_resubmission(tmp_path, capsys):
    workspace, _, request = fixture(tmp_path, "en")
    adapter = InterruptedAdapter()
    register_mandarin_adapter("interrupted", adapter)
    data = json.loads(request.read_text()); data.update(adapter="interrupted", authorization_ref="approved-42")
    request.write_text(json.dumps(data))

    _, uncertain = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    _, shown = invoke(capsys, "operation", "show", uncertain["operation_id"], "--workspace", str(workspace))
    _, resumed = invoke(capsys, "operation", "resume", uncertain["operation_id"], "--workspace", str(workspace))
    _, reconciled = invoke(capsys, "operation", "reconcile", uncertain["operation_id"], "--workspace", str(workspace))

    assert uncertain["status"] == shown["status"] == resumed["status"] == reconciled["status"] == "uncertain"
    assert adapter.calls == [adapter.calls[0]]
    assert reconciled["next_action"]["type"] == "reconcile"
    receipt = json.loads((tmp_path / "results/operation-receipts" / f'{uncertain["operation_id"]}.json').read_text())
    assert receipt["reconciliation"][-1]["state"] == "committed"


def test_reconcile_verified_paid_result_completes_without_resubmission(tmp_path, capsys):
    workspace, _, request = fixture(tmp_path, "en")
    adapter = RecoverableInterruptedAdapter(); register_mandarin_adapter("recoverable", adapter)
    data = json.loads(request.read_text()); data.update(adapter="recoverable", authorization_ref="approved-43")
    request.write_text(json.dumps(data))
    _, uncertain = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))

    code, recovered = invoke(capsys, "operation", "reconcile", uncertain["operation_id"], "--workspace", str(workspace))

    assert code == 0
    assert recovered["status"] == "completed"
    assert len(adapter.calls) == 1
    assert recovered["validation"]["audio_spec"] == "passed"
