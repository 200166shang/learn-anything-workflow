"""Independent, resumable production of a verified Mandarin listening edition."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .command_response import response
from .manifest import atomic_write_json, read_json
from .workspace import WorkspaceConfig, discover_workspace


CONTRACT_VERSION = 1
PROFILE = "alibaba-podcast-tts-throughput"
SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "schemas/mandarin-audio-request-v1.schema.json").read_text())


class MandarinAdapter(Protocol):
    adapter_identity: str
    authorization_category: str
    requires_authorization: bool

    def localize(self, source: Path, target: Path, *, profile: str,
                 idempotency_token: str) -> dict[str, Any]: ...

    def reconcile(self, attempt: dict[str, Any], target: Path) -> dict[str, Any]: ...


class PyVideoTransAdapter:
    adapter_identity = "pyvideotrans/podcast-v1"
    authorization_category = "paid_tts"
    requires_authorization = True

    def __init__(self, config: WorkspaceConfig):
        self.config = config

    def localize(self, source: Path, target: Path, *, profile: str,
                 idempotency_token: str) -> dict[str, Any]:
        python, cli = self.config.pyvideotrans_python, self.config.pyvideotrans_cli
        if not python or not cli or not python.is_file() or not cli.is_file():
            raise FileNotFoundError("pyVideoTrans public adapter is not configured")
        output_dir = target.parent
        output_dir.mkdir(parents=True, exist_ok=True)
        command = [str(python), str(cli), "--task", "podcast", "--name", str(source),
                   "--output-dir", str(output_dir), "--podcast-profile", profile,
                   "--idempotency-token", idempotency_token]
        completed = subprocess.run(command, text=True, capture_output=True)
        if completed.returncode:
            raise RuntimeError("pyVideoTrans submission outcome is uncertain; reconcile the original operation")
        return {"artifact": target, "query_handle": idempotency_token}

    def reconcile(self, attempt: dict[str, Any], target: Path) -> dict[str, Any]:
        if audio_info(target)["valid"]:
            return {"state": "available", "artifact": target,
                    "query_handle": attempt.get("query_handle")}
        return {"state": "unsupported", "query_handle": attempt.get("query_handle")}


MANDARIN_ADAPTERS: dict[str, MandarinAdapter] = {}


def register_mandarin_adapter(name: str, adapter: MandarinAdapter) -> None:
    MANDARIN_ADAPTERS[name] = adapter


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audio_info(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"valid": False}
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "format=duration,bit_rate:stream=sample_rate,channels,bit_rate", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if completed.returncode:
        return {"valid": False}
    try:
        raw = json.loads(completed.stdout)
        stream, fmt = (raw.get("streams") or [{}])[0], raw.get("format", {})
        bitrate = int(stream.get("bit_rate") or fmt.get("bit_rate") or 0)
        value = {"duration": float(fmt.get("duration") or 0),
                 "sample_rate": int(stream.get("sample_rate") or 0),
                 "channels": int(stream.get("channels") or 0), "bitrate": bitrate}
    except (TypeError, ValueError, json.JSONDecodeError, IndexError):
        return {"valid": False}
    value["valid"] = (value["duration"] > 0 and value["sample_rate"] == 48000
                      and value["channels"] == 1 and 56000 <= bitrate <= 76000)
    return value


def _transcript(package: Path, manifest: dict[str, Any]) -> tuple[Path | None, str]:
    for key in ("source_transcript", "source_subtitle"):
        raw = manifest.get("artifacts", {}).get(key)
        if isinstance(raw, str):
            path = (package / raw).resolve()
            try: path.relative_to(package)
            except ValueError: continue
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                text = re.sub(r"\d{1,2}:\d{2}(?::\d{2})?[,.]\d+\s*-->.*", " ", text)
                text = re.sub(r"<[^>]+>|\d+\s*\n", " ", text)
                return path, " ".join(text.split())
    return None, ""


def listening_quality(path: Path, package: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    info = audio_info(path)
    transcript_path, text = _transcript(package, manifest)
    if not info.get("valid") or not transcript_path or not text:
        return {"valid": False, "non_empty_audible": False, "transcript_alignment": "missing"}
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "8000",
                              "-f", "s16le", "-"], capture_output=True)
    samples = array("h"); samples.frombytes(decoded.stdout)
    if not samples:
        return {"valid": False, "non_empty_audible": False, "transcript_alignment": "failed"}
    peak = max(abs(value) for value in samples) or 1
    non_silent_ratio = sum(abs(value) > peak * .03 for value in samples) / len(samples)
    crossings = [index for index in range(1, len(samples)) if (samples[index - 1] < 0) != (samples[index] < 0)]
    intervals = [b - a for a, b in zip(crossings, crossings[1:])]
    variability = 0.0
    if intervals:
        mean = sum(intervals) / len(intervals)
        variability = math.sqrt(sum((value - mean) ** 2 for value in intervals) / len(intervals)) / max(mean, 1)
    tokens = re.findall(r"[A-Za-z0-9']+|[\u3400-\u9fff]", text)
    wpm = len(tokens) / info["duration"] * 60
    source_raw = manifest.get("artifacts", {}).get("source_audio")
    source_info = audio_info(package / source_raw) if isinstance(source_raw, str) else {"duration": 0}
    duration_ratio = info["duration"] / max(float(source_info.get("duration") or 0), .001)
    natural_proxy = non_silent_ratio >= .15 and variability >= .08 and 20 <= wpm <= 500 and .5 <= duration_ratio <= 2.5
    return {"valid": natural_proxy, "non_empty_audible": non_silent_ratio >= .15,
            "non_silent_ratio": round(non_silent_ratio, 4), "voicing_variability": round(variability, 4),
            "units_per_minute": round(wpm, 2), "duration_ratio_to_source": round(duration_ratio, 4),
            "transcript_alignment": "duration_and_rate_checked",
            "transcript_sha256": _sha256(transcript_path),
            "listening_review": {"model": "pending", "human": "pending"}}


def _normalized(request: dict[str, Any]) -> dict[str, Any]:
    try:
        Draft202012Validator(SCHEMA).validate(request)
    except ValidationError as exc:
        raise ValueError("invalid Mandarin audio request: " + exc.message) from exc
    return {**request, "profile": request.get("profile", PROFILE),
            "sample_rate": request.get("sample_rate", 48000), "channels": request.get("channels", 1),
            "bitrate_kbps": request.get("bitrate_kbps", 64), "adapter": request.get("adapter", "pyvideotrans")}


def _package(request: dict[str, Any]) -> tuple[Path, Path, dict[str, Any], str]:
    package = Path(request["package"]).expanduser().resolve()
    manifest = read_json(package / "manifest.json")
    if manifest.get("schema_version") != 5:
        raise ValueError("audio.mandarin requires a canonical schema-v5 media package")
    identity = manifest.get("identity")
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("canonical package identity is required")
    if request.get("source_id") != identity:
        raise ValueError("source_id must match the canonical package identity")
    raw = manifest.get("artifacts", {}).get("source_audio")
    if not isinstance(raw, str):
        raise ValueError("prepared public source_audio is required")
    source = (package / raw).resolve()
    try:
        source.relative_to(package)
    except ValueError as exc:
        raise ValueError("source_audio must be package-relative") from exc
    if not source.is_file():
        raise ValueError("prepared public source_audio is missing")
    provenance = manifest.get("provenance", {}).get("source_audio", {})
    language = str(provenance.get("language") or "unknown").strip().casefold().replace("_", "-")
    return package, source, manifest, language


def _operation_path(config: WorkspaceConfig, operation_id: str) -> Path:
    assert config.local is not None
    return config.local / "operations" / f"{operation_id}.json"


def _operation_id(config: WorkspaceConfig, request: dict[str, Any], manifest: dict[str, Any]) -> str:
    logical = {"workspace_id": config.workspace_id, "capability_id": "audio.mandarin",
               "contract_version": CONTRACT_VERSION, "package_identity": manifest.get("identity"),
               "source_id": request["source_id"],
               "source_version": request["source_version"],
               "effective_parameters": {key: request[key] for key in
                                        ("profile", "sample_rate", "channels", "bitrate_kbps", "adapter")}}
    return "operation-" + hashlib.sha256(json.dumps(logical, sort_keys=True).encode()).hexdigest()[:24]


def operation_identity(request: dict[str, Any]) -> str:
    """Return the public identity after applying all effective defaults."""
    normalized = _normalized(request)
    config = discover_workspace(Path(normalized["workspace"]))
    _, _, manifest, _ = _package(normalized)
    return _operation_id(config, normalized, manifest)


def _adapter(config: WorkspaceConfig, name: str) -> MandarinAdapter | None:
    return PyVideoTransAdapter(config) if name == "pyvideotrans" else MANDARIN_ADAPTERS.get(name)


def _public(config: WorkspaceConfig, record: dict[str, Any], reuse: str | None = None) -> dict[str, Any]:
    public_status = record["status"]
    if public_status == "running":
        from .media_operations import _lease_active
        public_status = "busy" if _lease_active(record) else "uncertain"
    return response(status=public_status, workspace=str(config.config_path), operation_id=record["operation_id"],
                    result={"reuse": reuse, "source_version": record["intent"]["source_version"]},
                    artifact_refs=record.get("artifact_refs", []), validation=record.get("validation", {}),
                    provenance=record.get("provenance", {}), next_action=record.get("next_action"),
                    diagnostics=record.get("diagnostics", []))


def _receipt(config: WorkspaceConfig, record: dict[str, Any]) -> None:
    assert config.results is not None
    intent = {key: value for key, value in record["intent"].items() if key != "requires_authorization"}
    atomic_write_json(config.results / "operation-receipts" / f'{record["operation_id"]}.json', {
        "schema_version": 1, "operation_id": record["operation_id"], "status": record["status"],
        "authoritative_revision": record.get("commit", {}).get("revision"),
        "authoritative_digest": record.get("commit", {}).get("digest"),
        "intent": intent, "artifact_facts": record.get("artifact_facts", {}),
        "attempts": record.get("attempts", []), "reconciliation": record.get("reconciliation", []),
    })


def _complete_authority(path: Path, record: dict[str, Any]) -> None:
    from .media_operations import _commit_authority
    _commit_authority(path, record)


def _language_primary(language: str) -> str | None:
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", language):
        return None
    primary = language.split("-", 1)[0]
    return primary if primary in {"zh", "en"} else None


def _finish(config: WorkspaceConfig, path: Path, record: dict[str, Any], source: Path,
            output: Path, mode: str, receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = audio_info(output)
    package = Path(record["intent"]["package"])
    quality = listening_quality(output, package, read_json(package / "manifest.json"))
    if not spec["valid"] or not quality["valid"]:
        uncertain = mode == "localized"
        record.update(status="uncertain" if uncertain else "recoverable_failure",
                      validation={"source": "passed", "audio_spec": "passed" if spec["valid"] else "failed",
                                  "listening_quality": "passed" if quality["valid"] else "failed"},
                      diagnostics=["Mandarin MP3 did not satisfy the 48 kHz mono approximately 64 kbps contract"],
                      next_action={"type": "reconcile" if uncertain else "resume",
                                   "operation_id": record["operation_id"]})
        atomic_write_json(path, record)
        return _public(config, record)
    output_digest = _sha256(output)
    if isinstance(record.get("current_attempt"), dict):
        record.setdefault("attempts", []).append({**record.pop("current_attempt"), "result": "verified_output"})
    record.update(status="completed", validation={"source": "passed", "audio_spec": "passed",
                                                   "listening_quality": "passed", "output_digest": "passed"},
                  artifact_refs=[str(output)], artifact_facts={"source": {"sha256": _sha256(source)},
                  "output": {"sha256": output_digest, "size": output.stat().st_size,
                             "validation": "ffprobe-48khz-mono-approx64kbps", "quality": quality}},
                  provenance={"capability_id": "audio.mandarin", "capability_contract_version": CONTRACT_VERSION,
                              "source_id": record["intent"]["source_id"],
                              "source_version": record["intent"]["source_version"], "mode": mode},
                  diagnostics=[], next_action=None)
    if receipt:
        record.setdefault("attempts", [])[-1]["receipt"] = {
            key: receipt[key] for key in ("receipt_id", "ledger_digest") if isinstance(receipt.get(key), str)
        }
    report = {"schema_version": 1, "mode": mode, "source_version": record["intent"]["source_version"],
              "source_sha256": record["artifact_facts"]["source"]["sha256"],
              "effective_parameters": record["intent"]["effective_parameters"],
              "output": "podcast.zh-CN.mp3", "output_sha256": output_digest, "spec": spec,
              "capability_contract_version": CONTRACT_VERSION, "listening_quality": quality}
    atomic_write_json(output.parent / "production-report.json", report)
    _complete_authority(path, record)
    _receipt(config, record)
    return _public(config, record)


def _run_mandarin_audio_unlocked(request: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalized(request)
    config = discover_workspace(Path(normalized["workspace"]))
    if config.schema_version != 2:
        raise ValueError("audio.mandarin requires workspace schema v2")
    package, source, manifest, language = _package(normalized)
    operation_id = _operation_id(config, normalized, manifest)
    path = _operation_path(config, operation_id)
    output = package / "listening/zh-CN/podcast.zh-CN.mp3"
    existing = read_json(path) if path.is_file() else None
    source_digest = _sha256(source)
    if normalized.get("check_only"):
        quality = listening_quality(output, package, manifest)
        valid = audio_info(output).get("valid") and quality.get("valid")
        return response(status="completed" if valid else "recoverable_failure",
                        workspace=str(config.config_path), operation_id=operation_id,
                        result={"package": str(package), "audio": str(output), "listening_quality": quality},
                        artifact_refs=[str(output)] if output.is_file() else [],
                        validation={"source": "passed", "audio_spec": "passed" if audio_info(output).get("valid") else "failed",
                                    "listening_quality": "passed" if quality.get("valid") else "failed"},
                        provenance={"capability_id": "audio.mandarin", "capability_contract_version": CONTRACT_VERSION,
                                    "source_id": normalized["source_id"], "source_version": normalized["source_version"]},
                        next_action=None if valid else {"type": "resume", "operation_id": operation_id})
    if existing:
        if existing["intent"]["source_sha256"] != source_digest:
            existing.update(status="missing_input", validation={"source": "failed"},
                            diagnostics=["source content changed without a new source_version"],
                            next_action={"type": "user", "reason": "provide the revised source_version"})
            return _public(config, existing)
        if existing.get("status") == "completed":
            from .media_operations import _authority_digest
            if existing.get("commit", {}).get("digest") != _authority_digest(existing):
                existing.update(status="uncertain", diagnostics=["authoritative operation digest is invalid"],
                                next_action={"type": "maintenance"})
                return _public(config, existing)
            facts = existing.get("artifact_facts", {}).get("output", {})
            if audio_info(output)["valid"] and facts.get("sha256") == _sha256(output) and facts.get("quality", {}).get("valid"):
                return _public(config, existing, "verified_operation")
        if existing.get("status") == "running":
            from .media_operations import _lease_active
            existing.update(status="busy" if _lease_active(existing) else "uncertain",
                            diagnostics=[] if _lease_active(existing) else ["previous paid worker lease expired; reconcile before retry"],
                            next_action=None if _lease_active(existing) else {"type": "reconcile", "operation_id": operation_id})
            atomic_write_json(path, existing)
            return _public(config, existing)
        if existing.get("status") == "uncertain":
            return _public(config, existing)
    primary = _language_primary(language)
    native = primary == "zh" and manifest.get("provenance", {}).get("source_audio", {}).get("kind") == "native_chinese_track"
    base_intent = {"capability_id": "audio.mandarin", "capability_contract_version": CONTRACT_VERSION,
                   "source_id": normalized["source_id"], "source_version": normalized["source_version"],
                   "source_sha256": source_digest, "package": str(package), "package_identity": manifest["identity"],
                   "effective_parameters": {key: normalized[key] for key in ("profile", "sample_rate", "channels", "bitrate_kbps", "adapter")}}
    saved_request = {key: normalized[key] for key in ("contract_version", "package", "source_id", "source_version", "profile",
                                                       "sample_rate", "channels", "bitrate_kbps", "adapter")}
    if primary is None:
        record = existing or {"schema_version": 1, "operation_id": operation_id, "intent": base_intent,
                              "request": saved_request, "attempts": []}
        record.update(status="unsupported", validation={"source_language": "failed"},
                      provenance={"capability_id": "audio.mandarin", "capability_contract_version": CONTRACT_VERSION,
                                  "source_id": normalized["source_id"], "source_version": normalized["source_version"]},
                      diagnostics=[f"unsupported source language for Mandarin localization: {language}"], next_action=None)
        _complete_authority(path, record)
        return _public(config, record)
    adapter = None if native else _adapter(config, normalized["adapter"])
    adapter_identity = "builtin/native-normalization-v1" if native else getattr(adapter, "adapter_identity", normalized["adapter"])
    intent = {**base_intent,
              "adapter_identity": adapter_identity, "authorization_category": "local_workspace" if native else getattr(adapter, "authorization_category", "paid_tts"),
              "requires_authorization": False if native else bool(getattr(adapter, "requires_authorization", True))}
    if normalized.get("authorization_ref"):
        saved_request["authorization_ref"] = normalized["authorization_ref"]
    record = existing or {"schema_version": 1, "operation_id": operation_id, "intent": intent,
                          "request": saved_request, "attempts": []}
    if existing and not native and existing.get("intent", {}).get("adapter_identity") != adapter_identity:
        record.update(status="uncertain", diagnostics=["pinned adapter identity is unavailable or changed; do not resubmit"],
                      next_action={"type": "reconcile", "operation_id": operation_id})
        atomic_write_json(path, record)
        return _public(config, record)
    if normalized.get("authorization_ref") and not record["intent"].get("authorization_ref"):
        record["intent"]["authorization_ref"] = normalized["authorization_ref"]
        record["request"]["authorization_ref"] = normalized["authorization_ref"]
    if record["intent"]["requires_authorization"] and not record["intent"].get("authorization_ref"):
        record.update(status="awaiting_user", validation={"authorization": "failed"},
                      diagnostics=["paid_tts requires a non-sensitive authorization_ref"],
                      next_action={"type": "user", "reason": "provide an authorization reference, never credentials"})
        atomic_write_json(path, record)
        return _public(config, record)
    output.parent.mkdir(parents=True, exist_ok=True)
    if native:
        try:
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-vn",
                            "-ar", str(normalized["sample_rate"]), "-ac", str(normalized["channels"]),
                            "-b:a", f'{normalized["bitrate_kbps"]}k', str(output)], check=True)
        except subprocess.CalledProcessError:
            record.update(status="recoverable_failure", validation={"source": "passed", "audio_spec": "failed"},
                          diagnostics=["local Mandarin audio normalization failed"],
                          next_action={"type": "resume", "operation_id": operation_id})
            atomic_write_json(path, record)
            return _public(config, record)
        return _finish(config, path, record, source, output, "native")
    if adapter is None:
        record.update(status="missing_dependency", validation={"adapter": "failed"},
                      diagnostics=["configured Mandarin adapter is unavailable"], next_action={"type": "maintenance"})
        atomic_write_json(path, record)
        return _public(config, record)
    token = hashlib.sha256(f"{operation_id}:localize".encode()).hexdigest()
    from .media_operations import _new_lease
    fencing = int(record.get("fencing", 0)) + 1
    lease = _new_lease(fencing)
    attempt = {"adapter_id": normalized["adapter"], "adapter_identity": adapter_identity,
               "idempotency_token": token, "query_handle": token, "submitted_at": datetime.now(timezone.utc).isoformat(),
               "owner": lease["owner"], "fencing": fencing}
    record["current_attempt"] = attempt
    record.update(status="running", fencing=fencing, lease=lease, diagnostics=[], next_action=None)
    atomic_write_json(path, record)
    try:
        result = adapter.localize(source, output, profile=normalized["profile"], idempotency_token=token)
    except Exception as exc:
        record.update(status="uncertain", diagnostics=[str(exc)],
                      next_action={"type": "reconcile", "operation_id": operation_id})
        record.pop("lease", None)
        atomic_write_json(path, record)
        return _public(config, record)
    from .media_operations import _owns_lease
    if not _owns_lease(path, lease["owner"], fencing):
        latest = read_json(path)
        latest.update(status="uncertain", diagnostics=["stale fenced paid result was rejected"],
                      next_action={"type": "reconcile", "operation_id": operation_id})
        atomic_write_json(path, latest)
        return _public(config, latest)
    attempt["query_handle"] = result.get("query_handle", token)
    record.pop("lease", None)
    return _finish(config, path, record, source, output, "localized", result.get("receipt"))


def run_mandarin_audio(request: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalized(request)
    config = discover_workspace(Path(normalized["workspace"]))
    package, _, manifest, _ = _package(normalized)
    operation_id = _operation_id(config, normalized, manifest)
    from .media_operations import _lock
    with _lock(config, operation_id) as acquired:
        if not acquired:
            path = _operation_path(config, operation_id)
            if path.is_file():
                record = read_json(path); record["status"] = "busy"; return _public(config, record)
            return response(status="busy", workspace=str(config.config_path), operation_id=operation_id,
                            next_action={"type": "resume", "operation_id": operation_id})
        return _run_mandarin_audio_unlocked(request)


run_mandarin_audio.__capability_contract__ = {
    "input_type": "mandarin-audio-request-v1", "output_type": "command-response-v1"
}


def show_operation(config: WorkspaceConfig, operation_id: str) -> dict[str, Any]:
    path = _operation_path(config, operation_id)
    if not path.is_file():
        return response(status="missing_input", workspace=str(config.config_path), operation_id=operation_id,
                        validation={"operation": "failed"}, diagnostics=["unknown operation_id"])
    record = read_json(path)
    if record.get("commit"):
        from .media_operations import _authority_digest
        if record["commit"].get("digest") != _authority_digest(record):
            record.update(status="uncertain", validation={**record.get("validation", {}), "authoritative_record": "failed"},
                          diagnostics=["authoritative operation digest is invalid"], next_action={"type": "maintenance"})
    return _public(config, record)


def resume_operation(config: WorkspaceConfig, operation_id: str) -> dict[str, Any]:
    path = _operation_path(config, operation_id)
    if not path.is_file():
        return show_operation(config, operation_id)
    record = read_json(path)
    if record.get("status") == "uncertain":
        record["next_action"] = {"type": "reconcile", "operation_id": operation_id,
                                 "reason": "query the original paid submission before any retry"}
        return _public(config, record)
    if record.get("status") == "recoverable_failure" and isinstance(record.get("request"), dict):
        request = {**record["request"], "workspace": str(config.config_path)}
        return run_mandarin_audio(request)
    return _public(config, record)


def _reconcile_operation_unlocked(config: WorkspaceConfig, operation_id: str) -> dict[str, Any]:
    path = _operation_path(config, operation_id)
    record = read_json(path)
    attempt = record.get("current_attempt")
    if not isinstance(attempt, dict):
        return _public(config, record)
    adapter = _adapter(config, attempt["adapter_id"])
    if adapter is None or getattr(adapter, "adapter_identity", None) != attempt.get("adapter_identity"):
        record.update(status="uncertain", diagnostics=["pinned adapter identity is unavailable or changed; do not resubmit"],
                      next_action={"type": "user", "reason": "restore the pinned adapter to query the original request"})
        atomic_write_json(path, record)
        return _public(config, record)
    output = Path(record.get("artifact_refs", [Path(record["intent"]["package"]) / "listening/zh-CN/podcast.zh-CN.mp3"])[0])
    try:
        checked = adapter.reconcile(attempt, output)
    except Exception as exc:
        record.update(status="uncertain", diagnostics=[f"adapter result query failed: {exc}"],
                      next_action={"type": "reconcile", "operation_id": operation_id})
        _complete_authority(path, record); _receipt(config, record)
        return _public(config, record)
    safe = {key: checked.get(key) for key in ("state", "query_handle")}
    record.setdefault("reconciliation", []).append(safe)
    if checked.get("state") in {"not_submitted", "retry_safe"}:
        record.update(status="recoverable_failure", diagnostics=[],
                      next_action={"type": "resume", "operation_id": operation_id})
        record.pop("lease", None)
        _complete_authority(path, record); _receipt(config, record)
        return _public(config, record)
    if checked.get("state") != "available":
        record.update(status="uncertain", diagnostics=["original paid submission cannot be verified; do not resubmit"],
                      next_action={"type": "reconcile", "operation_id": operation_id})
        record.pop("lease", None)
        _complete_authority(path, record); _receipt(config, record)
        return _public(config, record)
    package = Path(record["intent"]["package"])
    manifest = read_json(package / "manifest.json")
    source_raw = manifest.get("artifacts", {}).get("source_audio")
    if not isinstance(source_raw, str):
        record.update(status="uncertain", diagnostics=["reconciled output has no verifiable public source"],
                      next_action={"type": "user", "reason": "restore the prepared public source package"})
        atomic_write_json(path, record); _receipt(config, record)
        return _public(config, record)
    record.setdefault("attempts", []).append({**record.pop("current_attempt"), "result": "reconciled_available"})
    record.pop("lease", None)
    return _finish(config, path, record, package / source_raw, output, "localized", checked.get("receipt"))


def reconcile_operation(config: WorkspaceConfig, operation_id: str) -> dict[str, Any]:
    from .media_operations import _lock
    with _lock(config, operation_id) as acquired:
        if not acquired:
            path = _operation_path(config, operation_id)
            return _public(config, read_json(path)) if path.is_file() else show_operation(config, operation_id)
        return _reconcile_operation_unlocked(config, operation_id)
