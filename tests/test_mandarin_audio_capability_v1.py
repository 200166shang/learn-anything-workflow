import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from array import array
import hashlib
import math
from difflib import SequenceMatcher

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
                    "aevalsrc=0.12*sin(2*PI*(180+35*sin(2*PI*3*t))*t)+0.07*sin(2*PI*360*t):d=2.5:s=48000",
                    "-c:a", "aac", str(audio)], check=True)
    transcript = package / "media/transcript.txt"; transcript.write_text("This is a short spoken listening quality fixture with several useful words.")
    manifest = {"schema_version": 5, "identity": "fixture-audio", "artifacts": {"source_audio": "media/audio.source.m4a", "source_transcript": "media/transcript.txt"},
                "provenance": {"source_audio": {"language": language, "kind": "native_chinese_track" if language == "zh-CN" else "source_track"}}}
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"contract_version": 1, "workspace": str(workspace), "package": str(package), "source_id": "fixture-audio",
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
        return {"query_handle": idempotency_token, "artifact": target, "verification": fixture_verification(target),
                "receipt": {"receipt_id": "fixture-receipt"}}

    def reconcile(self, attempt, target):
        return {"state": "available", "query_handle": attempt["query_handle"], "artifact": target,
                "verification": fixture_verification(target),
                "receipt": {"receipt_id": "fixture-receipt"}}


def fixture_verification(target: Path, recognized_text: str | None = None) -> dict:
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(target), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True, check=True)
    samples = array("h"); samples.frombytes(decoded.stdout)
    crossings = [i for i in range(1, len(samples)) if (samples[i - 1] < 0) != (samples[i] < 0)]
    intervals = [b - a for a, b in zip(crossings, crossings[1:])]
    mean = sum(intervals) / max(len(intervals), 1)
    variability = math.sqrt(sum((x - mean) ** 2 for x in intervals) / max(len(intervals), 1)) / max(mean, 1)
    transcript = target.parents[2] / "media/transcript.txt"
    expected_text = transcript.read_text()
    recognized_text = expected_text if recognized_text is None else recognized_text
    confidence = .95 if variability >= .08 else .1
    content_confidence = SequenceMatcher(None, expected_text.casefold(), recognized_text.casefold()).ratio()
    return {"verifier_identity": "fixture-waveform-asr-v1", "transcript_sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(),
            "output_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "detected_language": "zh-CN",
            "speech_confidence": confidence, "alignment_confidence": confidence,
            "content_match_confidence": content_confidence}


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
                "verification": fixture_verification(target),
                "receipt": {"receipt_id": "recovered-receipt"}}


class CrashAdapter(FakePaidAdapter):
    adapter_identity = "fixture.mandarin-crash-v1"
    def localize(self, source, target, *, profile, idempotency_token):
        self.calls.append(idempotency_token)
        raise SystemExit("simulated process death")
    def reconcile(self, attempt, target):
        return {"state": "committed", "query_handle": attempt["query_handle"]}


class UnverifiedAdapter(FakePaidAdapter):
    adapter_identity = "fixture.mandarin-unverified-v1"
    def localize(self, source, target, *, profile, idempotency_token):
        self.calls.append(idempotency_token); target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-ar", "48000", "-ac", "1", "-b:a", "64k", str(target)], check=True)
        return {"query_handle": idempotency_token, "artifact": target}


class UnrelatedContentAdapter(FakePaidAdapter):
    adapter_identity = "fixture.mandarin-unrelated-v1"
    def localize(self, source, target, *, profile, idempotency_token):
        result = super().localize(source, target, profile=profile, idempotency_token=idempotency_token)
        result["verification"] = fixture_verification(target, "Completely unrelated weather forecast and sports scores.")
        return result


class ZeroTranscriptDigestAdapter(FakePaidAdapter):
    adapter_identity = "fixture.mandarin-zero-transcript-v1"
    def localize(self, source, target, *, profile, idempotency_token):
        result = super().localize(source, target, profile=profile, idempotency_token=idempotency_token)
        result["verification"]["transcript_sha256"] = "0" * 64
        return result


def test_native_chinese_normalizes_locally_and_reuses_only_verified_fingerprint(tmp_path, capsys):
    workspace, package, request = fixture(tmp_path, "zh-CN")
    adapter = FakePaidAdapter()
    register_mandarin_adapter("fixture-paid", adapter)

    code, first = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    code2, reused = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))

    assert code == code2 == 0
    assert adapter.calls == []
    assert reused["result"]["reuse"] == "verified_operation"
    assert reused["validation"]["localized_content"] == "not_required"
    output = package / "listening/zh-CN/podcast.zh-CN.mp3"
    assert output.is_file()
    report = json.loads((output.parent / "production-report.json").read_text())
    assert report["mode"] == "native"
    assert "authorization_ref" not in report

    # A format-valid replacement source must not cause reuse of old content.
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=880:duration=2.5", "-c:a", "aac", str(package / "media/audio.source.m4a")], check=True)
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
    _, shown = invoke(capsys, "operation", "show", result["operation_id"], "--workspace", str(tmp_path / "workspace.toml"))
    assert shown["api_version"] == 1 and shown["status"] == "unsupported"
    assert shown["provenance"]["capability_id"] == "audio.mandarin"


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
    _, repeated = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    _, shown = invoke(capsys, "operation", "show", uncertain["operation_id"], "--workspace", str(workspace))
    _, resumed = invoke(capsys, "operation", "resume", uncertain["operation_id"], "--workspace", str(workspace))
    _, reconciled = invoke(capsys, "operation", "reconcile", uncertain["operation_id"], "--workspace", str(workspace))

    assert uncertain["status"] == shown["status"] == resumed["status"] == reconciled["status"] == "uncertain"
    assert repeated["status"] == "uncertain"
    assert len(adapter.calls) == 1
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


def test_engineering_is_not_misparsed_as_english(tmp_path, capsys):
    _, _, request = fixture(tmp_path, "engineering")
    code, result = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert code == 1 and result["status"] == "unsupported"


def test_package_identity_prevents_cross_package_operation_collision(tmp_path, capsys):
    _, package, request = fixture(tmp_path, "zh-CN")
    _, first = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    other = tmp_path / "other"; __import__("shutil").copytree(package, other)
    manifest = json.loads((other / "manifest.json").read_text()); manifest["identity"] = "fixture-other"
    (other / "manifest.json").write_text(json.dumps(manifest))
    data = json.loads(request.read_text()); data.update(package=str(other), source_id="fixture-other")
    request.write_text(json.dumps(data))
    _, second = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert first["operation_id"] != second["operation_id"]


def test_native_chinese_without_transcript_uses_verified_provenance_and_never_tts(tmp_path, capsys):
    _, package, request = fixture(tmp_path, "zh-CN")
    (package / "media/transcript.txt").unlink()
    manifest = json.loads((package / "manifest.json").read_text()); manifest["artifacts"].pop("source_transcript")
    manifest["provenance"]["source_audio"]["kind"] = "source_track"
    (package / "manifest.json").write_text(json.dumps(manifest))
    code, result = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert code == 0 and result["status"] == "completed"
    assert result["validation"]["localized_content"] == "not_required"


def test_localized_dual_tone_fails_structured_speech_and_alignment_verification(tmp_path, capsys):
    _, package, request = fixture(tmp_path, "en")
    source = package / "media/audio.source.m4a"
    source.unlink()
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2.5", "-c:a", "aac", str(source)], check=True)
    adapter = FakePaidAdapter(); register_mandarin_adapter("tone", adapter)
    data = json.loads(request.read_text()); data.update(adapter="tone", authorization_ref="approved-tone")
    request.write_text(json.dumps(data))
    code, result = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert code == 3
    assert result["status"] == "awaiting_user"
    assert result["validation"]["localized_content"] == "failed"


def test_crashed_paid_worker_never_submits_again_before_reconcile(tmp_path, capsys):
    workspace, _, request = fixture(tmp_path, "en")
    adapter = CrashAdapter(); register_mandarin_adapter("crash", adapter)
    data = json.loads(request.read_text()); data.update(adapter="crash", authorization_ref="approved-crash")
    request.write_text(json.dumps(data))
    with pytest.raises(SystemExit):
        invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    operation = next((tmp_path / "local/operations").glob("operation-*.json"))
    record = json.loads(operation.read_text())
    record["lease"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    operation.write_text(json.dumps(record))

    _, uncertain = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    _, resumed = invoke(capsys, "operation", "resume", uncertain["operation_id"], "--workspace", str(workspace))

    assert uncertain["status"] == resumed["status"] == "uncertain"
    assert len(adapter.calls) == 1
    assert uncertain["next_action"]["type"] == "reconcile"


def test_localized_format_without_independent_verification_never_completes(tmp_path, capsys):
    _, package, request = fixture(tmp_path, "en")
    adapter = UnverifiedAdapter(); register_mandarin_adapter("unverified", adapter)
    data = json.loads(request.read_text()); data.update(adapter="unverified", authorization_ref="approved-unverified")
    request.write_text(json.dumps(data))
    code, result = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert code == 3 and result["status"] == "awaiting_user"
    assert result["validation"]["localized_content"] == "failed"
    _, repeated = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert repeated["status"] == "awaiting_user" and len(adapter.calls) == 1
    verification = tmp_path / "verification.json"
    verification.write_text(json.dumps(fixture_verification(package / "listening/zh-CN/podcast.zh-CN.mp3")))
    data["verification_report"] = str(verification); request.write_text(json.dumps(data))
    final_code, completed = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert final_code == 0 and completed["status"] == "completed" and len(adapter.calls) == 1


def test_structured_verifier_rejects_unrelated_recognized_content(tmp_path, capsys):
    _, _, request = fixture(tmp_path, "en")
    adapter = UnrelatedContentAdapter(); register_mandarin_adapter("unrelated", adapter)
    data = json.loads(request.read_text()); data.update(adapter="unrelated", authorization_ref="approved-unrelated")
    request.write_text(json.dumps(data))
    code, result = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert code == 3 and result["status"] == "awaiting_user"
    assert result["validation"]["localized_content"] == "failed"


def test_structured_verifier_rejects_placeholder_transcript_digest(tmp_path, capsys):
    _, _, request = fixture(tmp_path, "en")
    adapter = ZeroTranscriptDigestAdapter(); register_mandarin_adapter("zero-transcript", adapter)
    data = json.loads(request.read_text()); data.update(adapter="zero-transcript", authorization_ref="approved-zero")
    request.write_text(json.dumps(data))
    code, result = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert code == 3 and result["status"] == "awaiting_user"
    assert result["validation"]["localized_content"] == "failed"


def test_matching_public_transcript_digest_is_authoritative_and_change_invalidates_reuse(tmp_path, capsys):
    workspace, package, request = fixture(tmp_path, "en")
    (package / "media/subtitle.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nLower-priority subtitle text.\n")
    manifest = json.loads((package / "manifest.json").read_text())
    manifest["artifacts"]["source_subtitle"] = "media/subtitle.srt"
    (package / "manifest.json").write_text(json.dumps(manifest))
    adapter = FakePaidAdapter(); register_mandarin_adapter("matching-transcript", adapter)
    data = json.loads(request.read_text()); data.update(adapter="matching-transcript", authorization_ref="approved-match")
    request.write_text(json.dumps(data))
    code, completed = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert code == 0 and completed["status"] == "completed"
    operation = json.loads(next((tmp_path / "local/operations").glob("operation-*.json")).read_text())
    expected = hashlib.sha256((package / "media/transcript.txt").read_bytes()).hexdigest()
    assert operation["artifact_facts"]["source_transcript"] == {
        "artifact": "source_transcript", "path": "media/transcript.txt", "sha256": expected,
    }

    (package / "media/transcript.txt").write_text("A revised public transcript changes the alignment basis.")
    _, shown = invoke(capsys, "operation", "show", completed["operation_id"], "--workspace", str(workspace))
    receipt = json.loads((tmp_path / "results/operation-receipts" / f'{completed["operation_id"]}.json').read_text())
    persisted = json.loads(next((tmp_path / "local/operations").glob("operation-*.json")).read_text())
    assert shown["status"] == receipt["status"] == persisted["status"] == "awaiting_user"
    assert shown["validation"]["source_transcript"] == "verification_stale"
    assert receipt["authoritative_revision"] == persisted["commit"]["revision"] == 2
    changed_code, changed = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert changed_code == 3 and changed["status"] == "awaiting_user" and len(adapter.calls) == 1

    verification = tmp_path / "revised-verification.json"
    verification.write_text(json.dumps(fixture_verification(package / "listening/zh-CN/podcast.zh-CN.mp3")))
    data["verification_report"] = str(verification); request.write_text(json.dumps(data))
    final_code, final = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert final_code == 0 and final["status"] == "completed" and len(adapter.calls) == 1


def test_new_source_version_requires_explicit_safe_adoption_and_never_repays(tmp_path, capsys):
    _, package, request = fixture(tmp_path, "en")
    adapter = FakePaidAdapter(); register_mandarin_adapter("version-adopt", adapter)
    data = json.loads(request.read_text()); data.update(adapter="version-adopt", authorization_ref="approved-version")
    request.write_text(json.dumps(data))
    _, first = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert first["status"] == "completed" and len(adapter.calls) == 1

    (package / "media/transcript.txt").write_text("The registered second version has a revised public transcript.")
    data["source_version"] = "source-version-2"; request.write_text(json.dumps(data))
    waiting_code, waiting = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert waiting_code == 3 and waiting["status"] == "awaiting_user" and len(adapter.calls) == 1
    assert first["operation_id"] in waiting["next_action"]["eligible_operation_ids"]

    verification = tmp_path / "version-2-verification.json"
    verification.write_text(json.dumps(fixture_verification(package / "listening/zh-CN/podcast.zh-CN.mp3")))
    data.update(adopt_operation_id=first["operation_id"], verification_report=str(verification))
    request.write_text(json.dumps(data))
    adopted_code, adopted = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert adopted_code == 0 and adopted["status"] == "completed" and len(adapter.calls) == 1
    assert adopted["provenance"]["adopted_operation_id"] == first["operation_id"]


def test_pyvideotrans_argparse_rejection_is_definitely_not_submitted(tmp_path, capsys):
    workspace, _, request = fixture(tmp_path, "en")
    cli = tmp_path / "fake-pyvideotrans.py"
    cli.write_text('''import sys
if "--help" in sys.argv:
    print("--task --name --output-dir --podcast-profile --report --resume")
    raise SystemExit(0)
sys.stderr.write("usage: cli.py [-h]\\ncli.py: error: invalid arguments\\n")
raise SystemExit(2)
''')
    with workspace.open("a") as stream:
        stream.write(f'\n[tools.pyvideotrans]\npython = "{__import__("sys").executable}"\ncli = "{cli}"\n')
    data = json.loads(request.read_text()); data["authorization_ref"] = "approved-parser"
    request.write_text(json.dumps(data))
    code, result = invoke(capsys, "capability", "run", "audio.mandarin", "--request", str(request))
    assert code == 3 and result["status"] == "missing_dependency"
    record = json.loads(next((tmp_path / "local/operations").glob("operation-*.json")).read_text())
    assert record["attempts"][-1]["result"] == "not_submitted"
