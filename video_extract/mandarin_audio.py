"""Independent, resumable production of a verified Mandarin listening edition."""

from __future__ import annotations

import hashlib
import json
import subprocess
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
                   "--output-dir", str(output_dir), "--podcast-profile", profile]
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
    language = str(provenance.get("language") or "unknown").casefold().replace("_", "-")
    return package, source, manifest, language


def _operation_path(config: WorkspaceConfig, operation_id: str) -> Path:
    assert config.local is not None
    return config.local / "operations" / f"{operation_id}.json"


def _operation_id(config: WorkspaceConfig, request: dict[str, Any], manifest: dict[str, Any]) -> str:
    logical = {"workspace_id": config.workspace_id, "capability_id": "audio.mandarin",
               "contract_version": CONTRACT_VERSION, "package_identity": manifest.get("identity"),
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
    return response(status=record["status"], workspace=str(config.config_path), operation_id=record["operation_id"],
                    result={"reuse": reuse, "source_version": record["intent"]["source_version"]},
                    artifact_refs=record.get("artifact_refs", []), validation=record.get("validation", {}),
                    provenance=record.get("provenance", {}), next_action=record.get("next_action"),
                    diagnostics=record.get("diagnostics", []))


def _receipt(config: WorkspaceConfig, record: dict[str, Any]) -> None:
    assert config.results is not None
    intent = {key: value for key, value in record["intent"].items() if key != "requires_authorization"}
    atomic_write_json(config.results / "operation-receipts" / f'{record["operation_id"]}.json', {
        "schema_version": 1, "operation_id": record["operation_id"], "status": record["status"],
        "intent": intent, "artifact_facts": record.get("artifact_facts", {}),
        "attempts": record.get("attempts", []), "reconciliation": record.get("reconciliation", []),
    })


def _finish(config: WorkspaceConfig, path: Path, record: dict[str, Any], source: Path,
            output: Path, mode: str, receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = audio_info(output)
    if not spec["valid"]:
        uncertain = mode == "localized"
        record.update(status="uncertain" if uncertain else "recoverable_failure",
                      validation={"source": "passed", "audio_spec": "failed"},
                      diagnostics=["Mandarin MP3 did not satisfy the 48 kHz mono approximately 64 kbps contract"],
                      next_action={"type": "reconcile" if uncertain else "resume",
                                   "operation_id": record["operation_id"]})
        atomic_write_json(path, record)
        return _public(config, record)
    output_digest = _sha256(output)
    record.update(status="completed", validation={"source": "passed", "audio_spec": "passed", "output_digest": "passed"},
                  artifact_refs=[str(output)], artifact_facts={"source": {"sha256": _sha256(source)},
                  "output": {"sha256": output_digest, "size": output.stat().st_size,
                             "validation": "ffprobe-48khz-mono-approx64kbps"}},
                  provenance={"capability_id": "audio.mandarin", "capability_contract_version": CONTRACT_VERSION,
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
              "capability_contract_version": CONTRACT_VERSION}
    atomic_write_json(output.parent / "production-report.json", report)
    atomic_write_json(path, record)
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
    if existing:
        if existing["intent"]["source_sha256"] != source_digest:
            existing.update(status="missing_input", validation={"source": "failed"},
                            diagnostics=["source content changed without a new source_version"],
                            next_action={"type": "user", "reason": "provide the revised source_version"})
            return _public(config, existing)
        if existing.get("status") == "completed":
            facts = existing.get("artifact_facts", {}).get("output", {})
            if audio_info(output)["valid"] and facts.get("sha256") == _sha256(output):
                return _public(config, existing, "verified_operation")
        if existing.get("status") == "uncertain":
            return _public(config, existing)
    native = language.startswith("zh") or manifest.get("provenance", {}).get("source_audio", {}).get("kind") == "native_chinese_track"
    if not native and not language.startswith("en"):
        record = existing or {"schema_version": 1, "operation_id": operation_id,
                              "intent": {"source_version": normalized["source_version"]}}
        record.update(status="unsupported", validation={"source_language": "failed"},
                      diagnostics=[f"unsupported source language for Mandarin localization: {language}"], next_action=None)
        atomic_write_json(path, record)
        return _public(config, record)
    adapter = None if native else _adapter(config, normalized["adapter"])
    adapter_identity = "builtin/native-normalization-v1" if native else getattr(adapter, "adapter_identity", normalized["adapter"])
    intent = {"capability_id": "audio.mandarin", "capability_contract_version": CONTRACT_VERSION,
              "source_version": normalized["source_version"], "source_sha256": source_digest,
              "package": str(package),
              "adapter_identity": adapter_identity, "authorization_category": "local_workspace" if native else getattr(adapter, "authorization_category", "paid_tts"),
              "requires_authorization": False if native else bool(getattr(adapter, "requires_authorization", True)),
              "effective_parameters": {key: normalized[key] for key in ("profile", "sample_rate", "channels", "bitrate_kbps", "adapter")}}
    saved_request = {key: normalized[key] for key in ("contract_version", "package", "source_version", "profile",
                                                       "sample_rate", "channels", "bitrate_kbps", "adapter")}
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
    attempt = {"adapter_id": normalized["adapter"], "adapter_identity": adapter_identity,
               "idempotency_token": token, "query_handle": token, "submitted_at": datetime.now(timezone.utc).isoformat()}
    record["current_attempt"] = attempt
    record.update(status="running", diagnostics=[], next_action=None)
    atomic_write_json(path, record)
    try:
        result = adapter.localize(source, output, profile=normalized["profile"], idempotency_token=token)
    except Exception as exc:
        record.update(status="uncertain", diagnostics=[str(exc)],
                      next_action={"type": "reconcile", "operation_id": operation_id})
        atomic_write_json(path, record)
        return _public(config, record)
    attempt["query_handle"] = result.get("query_handle", token)
    record.setdefault("attempts", []).append({**record.pop("current_attempt"), "result": "verified_output"})
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
    return _public(config, read_json(path))


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


def reconcile_operation(config: WorkspaceConfig, operation_id: str) -> dict[str, Any]:
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
    checked = adapter.reconcile(attempt, output)
    safe = {key: checked.get(key) for key in ("state", "query_handle")}
    record.setdefault("reconciliation", []).append(safe)
    if checked.get("state") != "available":
        record.update(status="uncertain", diagnostics=["original paid submission cannot be verified; do not resubmit"],
                      next_action={"type": "reconcile", "operation_id": operation_id})
        atomic_write_json(path, record); _receipt(config, record)
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
    return _finish(config, path, record, package / source_raw, output, "localized", checked.get("receipt"))
