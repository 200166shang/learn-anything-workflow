"""Independent, resumable production of a verified Mandarin listening edition."""

from __future__ import annotations

import hashlib
import json
import re
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
        probe = subprocess.run([str(python), str(cli), "--help"], text=True, capture_output=True)
        required = ("--task", "--name", "--output-dir", "--podcast-profile", "--report", "--resume")
        if probe.returncode or not all(flag in probe.stdout for flag in required):
            raise AdapterInvocationError("pyVideoTrans CLI contract is incompatible", submitted=False)
        command = [str(python), str(cli), "--task", "podcast", "--name", str(source),
                   "--output-dir", str(output_dir), "--podcast-profile", profile,
                   "--report", str(output_dir / "production-report.json")]
        completed = subprocess.run(command, text=True, capture_output=True)
        if completed.returncode:
            parser_failure = "usage:" in completed.stderr.casefold() and "error:" in completed.stderr.casefold()
            raise AdapterInvocationError(
                "pyVideoTrans rejected the public invocation" if parser_failure else
                "pyVideoTrans submission outcome is uncertain; reconcile the original operation",
                submitted=not parser_failure,
            )
        return {"artifact": target, "query_handle": idempotency_token}

    def reconcile(self, attempt: dict[str, Any], target: Path) -> dict[str, Any]:
        report = target.parent / "production-report.json"
        if audio_info(target)["valid"] and report.is_file():
            return {"state": "available", "artifact": target,
                    "query_handle": attempt.get("query_handle")}
        return {"state": "unsupported", "query_handle": attempt.get("query_handle")}


class AdapterInvocationError(RuntimeError):
    def __init__(self, message: str, *, submitted: bool):
        super().__init__(message)
        self.submitted = submitted


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


def _public_transcript_fact(package: Path, manifest: dict[str, Any]) -> dict[str, str] | None:
    for key in ("source_transcript", "source_subtitle"):
        raw = manifest.get("artifacts", {}).get(key)
        if not isinstance(raw, str):
            continue
        path = (package / raw).resolve()
        try:
            path.relative_to(package)
        except ValueError:
            continue
        if path.is_file():
            return {"artifact": key, "path": raw, "sha256": _sha256(path)}
    return None


def _localized_verification(value: Any, output: Path, transcript_fact: dict[str, str] | None) -> dict[str, Any]:
    if transcript_fact is None:
        return {"valid": False, "reason": "a public source_transcript or source_subtitle is required for localized alignment"}
    if not isinstance(value, dict):
        return {"valid": False, "reason": "independent localized verification is missing"}
    required_strings = ("verifier_identity", "transcript_sha256", "output_sha256", "detected_language")
    if any(not isinstance(value.get(key), str) or not value[key] for key in required_strings):
        return {"valid": False, "reason": "localized verification fields are incomplete"}
    language = value["detected_language"].casefold().replace("_", "-").split("-", 1)[0]
    digest_ok = value["output_sha256"] == _sha256(output)
    transcript_ok = value["transcript_sha256"] == transcript_fact["sha256"]
    speech = value.get("speech_confidence")
    alignment = value.get("alignment_confidence")
    content = value.get("content_match_confidence")
    scores_ok = all(isinstance(score, (int, float)) and not isinstance(score, bool) and score >= .8
                    for score in (speech, alignment, content))
    safe = {key: value.get(key) for key in (*required_strings, "speech_confidence", "alignment_confidence",
                                             "content_match_confidence")}
    safe["valid"] = digest_ok and transcript_ok and language == "zh" and scores_ok
    safe["transcript_artifact"] = transcript_fact["artifact"]
    return safe


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


def _mark_verification_stale(config: WorkspaceConfig, path: Path, record: dict[str, Any]) -> bool:
    if record.get("status") != "completed" or record.get("provenance", {}).get("mode") != "localized":
        return False
    package = Path(record["intent"]["package"])
    current = _public_transcript_fact(package, read_json(package / "manifest.json"))
    recorded = record.get("artifact_facts", {}).get("source_transcript")
    if current is not None and isinstance(recorded, dict) and current.get("sha256") == recorded.get("sha256"):
        return False
    record.update(status="awaiting_user",
                  validation={**record.get("validation", {}), "source_transcript": "verification_stale",
                              "localized_content": "failed"},
                  diagnostics=["public transcript changed; localized verification is stale"],
                  next_action={"type": "user", "operation_id": record["operation_id"],
                               "reason": "provide an independent verification report matching the current public transcript"})
    _complete_authority(path, record)
    _receipt(config, record)
    return True


def _matching_prior_operations(config: WorkspaceConfig, operation_id: str, intent: dict[str, Any]) -> list[dict[str, Any]]:
    assert config.local is not None
    matches = []
    for path in (config.local / "operations").glob("operation-*.json"):
        if path.stem == operation_id:
            continue
        try: prior = read_json(path)
        except (OSError, json.JSONDecodeError): continue
        previous = prior.get("intent", {})
        if (previous.get("capability_id") == "audio.mandarin"
                and previous.get("package_identity") == intent.get("package_identity")
                and previous.get("source_sha256") == intent.get("source_sha256")
                and previous.get("effective_parameters") == intent.get("effective_parameters")
                and previous.get("adapter_identity") == intent.get("adapter_identity")
                and isinstance(prior.get("artifact_facts", {}).get("output", {}).get("sha256"), str)
                and prior.get("artifact_facts", {}).get("output", {}).get("size", 0) > 0
                and prior.get("artifact_facts", {}).get("output", {}).get("validation") == "ffprobe-48khz-mono-approx64kbps"):
            matches.append(prior)
    return matches


def _prior_authority(config: WorkspaceConfig, prior: dict[str, Any]) -> tuple[bool, str]:
    from .media_operations import _authority_digest
    commit = prior.get("commit")
    if prior.get("status") != "completed":
        return False, "prior operation is not completed"
    if (not isinstance(commit, dict) or not isinstance(commit.get("revision"), int)
            or commit["revision"] < 1 or not isinstance(commit.get("committed_at"), str)
            or not commit["committed_at"] or commit.get("digest") != _authority_digest(prior)):
        return False, "prior authoritative commit is invalid"
    assert config.results is not None
    receipt_path = config.results / "operation-receipts" / f'{prior.get("operation_id")}.json'
    if not receipt_path.is_file():
        return False, "prior authoritative receipt is missing"
    try: receipt = read_json(receipt_path)
    except (OSError, json.JSONDecodeError): return False, "prior authoritative receipt is invalid"
    if (receipt.get("status") != "completed"
            or receipt.get("authoritative_revision") != commit["revision"]
            or receipt.get("authoritative_digest") != commit["digest"]):
        return False, "prior receipt does not match the authoritative commit"
    return True, "verified authoritative operation and receipt"


def _adoption_candidates(config: WorkspaceConfig, operation_id: str, intent: dict[str, Any]) -> list[dict[str, Any]]:
    return [prior for prior in _matching_prior_operations(config, operation_id, intent)
            if _prior_authority(config, prior)[0]]


def _adopt_prior(config: WorkspaceConfig, path: Path, record: dict[str, Any], prior: dict[str, Any],
                 source: Path, output: Path, verification_path: str | None) -> dict[str, Any]:
    facts = prior.get("artifact_facts", {}).get("output", {})
    if (not output.is_file() or facts.get("sha256") != _sha256(output) or facts.get("size") != output.stat().st_size
            or facts.get("validation") != "ffprobe-48khz-mono-approx64kbps" or not audio_info(output).get("valid")
            or prior.get("intent", {}).get("adapter_identity") != record["intent"].get("adapter_identity")):
        record.update(status="uncertain", validation={"adopted_output": "failed"},
                      diagnostics=["prior operation output or adapter identity cannot be safely adopted"],
                      next_action={"type": "user", "reason": "select a verified prior operation"})
        _complete_authority(path, record); _receipt(config, record)
        return _public(config, record)
    record.update(status="awaiting_user", artifact_refs=[str(output)],
                  artifact_facts={"source": {"sha256": _sha256(source)}, "output": dict(facts)},
                  validation={"source": "passed", "audio_spec": "passed", "source_transcript": "verification_stale",
                              "localized_content": "failed", "adopted_output": "passed"},
                  provenance={"capability_id": "audio.mandarin", "capability_contract_version": CONTRACT_VERSION,
                              "source_id": record["intent"]["source_id"], "source_version": record["intent"]["source_version"],
                              "mode": "localized", "adopted_operation_id": prior["operation_id"]},
                  diagnostics=["prior paid output adopted; verification must match the current public transcript"],
                  next_action={"type": "user", "operation_id": record["operation_id"],
                               "reason": "provide a matching independent verification report"})
    record["intent"]["adopted_operation_id"] = prior["operation_id"]
    if verification_path:
        return _finish(config, path, record, source, output, "localized",
                       verification=read_json(Path(verification_path).expanduser().resolve()))
    _complete_authority(path, record); _receipt(config, record)
    return _public(config, record)


def _language_primary(language: str) -> str | None:
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", language):
        return None
    primary = language.split("-", 1)[0]
    return primary if primary in {"zh", "en"} else None


def _finish(config: WorkspaceConfig, path: Path, record: dict[str, Any], source: Path,
            output: Path, mode: str, receipt: dict[str, Any] | None = None,
            verification: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = audio_info(output)
    localized = mode == "localized"
    package = Path(record["intent"]["package"])
    manifest = read_json(package / "manifest.json")
    transcript_fact = _public_transcript_fact(package, manifest) if localized else None
    verified = _localized_verification(verification, output, transcript_fact) if localized and spec["valid"] else None
    if not spec["valid"] or (localized and not verified["valid"]):
        uncertain = mode == "localized"
        record.update(status="awaiting_user" if localized and spec["valid"] else ("uncertain" if uncertain else "recoverable_failure"),
                      validation={"source": "passed", "audio_spec": "passed" if spec["valid"] else "failed",
                                  "localized_content": "failed" if localized else "not_required"},
                      diagnostics=[verified.get("reason", "localized speech/language/content verification failed")
                                   if localized and spec["valid"] else
                                   "Mandarin MP3 did not satisfy the 48 kHz mono approximately 64 kbps contract"],
                      next_action={"type": "user" if localized and spec["valid"] else ("reconcile" if uncertain else "resume"),
                                   "operation_id": record["operation_id"],
                                   **({"reason": "provide an independent structured Mandarin ASR/alignment verification report"}
                                      if localized and spec["valid"] else {})})
        atomic_write_json(path, record)
        return _public(config, record)
    output_digest = _sha256(output)
    if isinstance(record.get("current_attempt"), dict):
        record.setdefault("attempts", []).append({**record.pop("current_attempt"), "result": "verified_output"})
    record.update(status="completed", validation={"source": "passed", "audio_spec": "passed",
                                                   "localized_content": "passed" if localized else "not_required",
                                                   "output_digest": "passed"},
                  artifact_refs=[str(output)], artifact_facts={"source": {"sha256": _sha256(source)},
                  "output": {"sha256": output_digest, "size": output.stat().st_size,
                             "validation": "ffprobe-48khz-mono-approx64kbps",
                             **({"localized_verification": verified} if verified else {})},
                  **({"source_transcript": transcript_fact} if transcript_fact else {})},
                  provenance={"capability_id": "audio.mandarin", "capability_contract_version": CONTRACT_VERSION,
                              "source_id": record["intent"]["source_id"],
                              "source_version": record["intent"]["source_version"], "mode": mode,
                              **({"adopted_operation_id": record.get("provenance", {}).get("adopted_operation_id")}
                                 if record.get("provenance", {}).get("adopted_operation_id") else {})},
                  diagnostics=[], next_action=None)
    if receipt:
        record.setdefault("attempts", [])[-1]["receipt"] = {
            key: receipt[key] for key in ("receipt_id", "ledger_digest") if isinstance(receipt.get(key), str)
        }
    report = {"schema_version": 1, "mode": mode, "source_version": record["intent"]["source_version"],
              "source_sha256": record["artifact_facts"]["source"]["sha256"],
              "effective_parameters": record["intent"]["effective_parameters"],
              "output": "podcast.zh-CN.mp3", "output_sha256": output_digest, "spec": spec,
              "capability_contract_version": CONTRACT_VERSION,
              "verification_basis": "verified_native_zh_provenance" if not localized else "independent_asr_alignment",
              **({"localized_verification": verified} if verified else {}),
              "listening_review": {"model": "pending", "human": "pending"}}
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
    primary = _language_primary(language)
    native = primary == "zh"
    base_intent = {"capability_id": "audio.mandarin", "capability_contract_version": CONTRACT_VERSION,
                   "source_id": normalized["source_id"], "source_version": normalized["source_version"],
                   "source_sha256": source_digest, "package": str(package), "package_identity": manifest["identity"],
                   "effective_parameters": {key: normalized[key] for key in ("profile", "sample_rate", "channels", "bitrate_kbps", "adapter")}}
    saved_request = {key: normalized[key] for key in ("contract_version", "package", "source_id", "source_version", "profile",
                                                       "sample_rate", "channels", "bitrate_kbps", "adapter")}
    if normalized.get("check_only"):
        spec_valid = bool(audio_info(output).get("valid"))
        report_path = output.parent / "production-report.json"
        report = read_json(report_path) if report_path.is_file() else {}
        transcript_fact = _public_transcript_fact(package, manifest) if primary != "zh" else None
        verification = (_localized_verification(report.get("localized_verification"), output, transcript_fact)
                        if spec_valid and primary != "zh" else None)
        valid = spec_valid and (primary == "zh" or bool(verification and verification.get("valid")))
        return response(status="completed" if valid else "recoverable_failure",
                        workspace=str(config.config_path), operation_id=operation_id,
                        result={"package": str(package), "audio": str(output),
                                "verification_basis": "verified_native_zh_provenance" if primary == "zh" else "independent_asr_alignment",
                                **({"localized_verification": verification} if verification else {})},
                        artifact_refs=[str(output)] if output.is_file() else [],
                        validation={"source": "passed", "audio_spec": "passed" if spec_valid else "failed",
                                    "localized_content": "not_required" if primary == "zh" else
                                                         ("passed" if verification and verification.get("valid") else "failed")},
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
            if _mark_verification_stale(config, path, existing):
                return _public(config, existing)
            facts = existing.get("artifact_facts", {}).get("output", {})
            content_state = existing.get("validation", {}).get("localized_content")
            if audio_info(output)["valid"] and facts.get("sha256") == _sha256(output) and content_state in {"passed", "not_required"}:
                return _public(config, existing, "verified_operation")
        if existing.get("status") == "running":
            from .media_operations import _lease_active
            existing.update(status="busy" if _lease_active(existing) else "uncertain",
                            diagnostics=[] if _lease_active(existing) else ["previous paid worker lease expired; reconcile before retry"],
                            next_action=None if _lease_active(existing) else {"type": "reconcile", "operation_id": operation_id})
            atomic_write_json(path, existing)
            return _public(config, existing)
        if existing.get("status") == "uncertain" and not normalized.get("adopt_operation_id"):
            return _public(config, existing)
        if existing.get("status") == "awaiting_user" and (isinstance(existing.get("current_attempt"), dict)
                or existing.get("validation", {}).get("source_transcript") == "verification_stale"):
            verification_path = normalized.get("verification_report")
            if not isinstance(verification_path, str):
                return _public(config, existing)
            verification = read_json(Path(verification_path).expanduser().resolve())
            existing.pop("lease", None)
            return _finish(config, path, existing, source, output, "localized", verification=verification)
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
    if not native and normalized.get("adopt_operation_id"):
        prior_path = _operation_path(config, normalized["adopt_operation_id"])
        prior = read_json(prior_path) if prior_path.is_file() else None
        matching = {candidate["operation_id"]: candidate for candidate in _matching_prior_operations(config, operation_id, intent)}
        authority_ok, authority_reason = _prior_authority(config, prior) if isinstance(prior, dict) else (False, "prior operation is missing")
        if not isinstance(prior, dict) or prior.get("operation_id") not in matching or not authority_ok:
            record.update(status="uncertain", validation={"adopted_output": "failed", "prior_authority": "failed"},
                          diagnostics=[f"selected prior operation cannot be safely adopted: {authority_reason}"],
                          next_action={"type": "user", "reason": "restore or select a completed operation with a matching authoritative receipt"})
            _complete_authority(path, record); _receipt(config, record)
            return _public(config, record)
        return _adopt_prior(config, path, record, prior, source, output, normalized.get("verification_report"))
    matching = _matching_prior_operations(config, operation_id, intent) if not native else []
    candidates = _adoption_candidates(config, operation_id, intent) if not native else []
    if existing is None and matching:
        rejected = [prior["operation_id"] for prior in matching if prior not in candidates]
        record.update(status="awaiting_user" if candidates else "uncertain",
                      validation={"adopted_output": "pending" if candidates else "failed",
                                  "prior_authority": "passed" if candidates else "failed"},
                      diagnostics=["a prior paid output is eligible for adoption; automatic resubmission is disabled"
                                   if candidates else "matching prior paid output has no trustworthy authority/receipt; automatic resubmission is disabled"],
                      next_action={"type": "user", "reason": "rerun with adopt_operation_id after reviewing the source-version change",
                                   "eligible_operation_ids": [candidate["operation_id"] for candidate in candidates],
                                   "rejected_operation_ids": rejected})
        _complete_authority(path, record); _receipt(config, record)
        return _public(config, record)
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
    except AdapterInvocationError as exc:
        record.update(status="uncertain" if exc.submitted else "missing_dependency", diagnostics=[str(exc)],
                      next_action={"type": "reconcile" if exc.submitted else "maintenance", "operation_id": operation_id})
        record.pop("lease", None)
        if not exc.submitted:
            record.setdefault("attempts", []).append({**record.pop("current_attempt"), "result": "not_submitted"})
        atomic_write_json(path, record)
        return _public(config, record)
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
    return _finish(config, path, record, source, output, "localized", result.get("receipt"), result.get("verification"))


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


def _show_operation_unlocked(config: WorkspaceConfig, operation_id: str) -> dict[str, Any]:
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
    if _mark_verification_stale(config, path, record):
        return _public(config, record)
    return _public(config, record)


def show_operation(config: WorkspaceConfig, operation_id: str) -> dict[str, Any]:
    from .media_operations import _lock
    with _lock(config, operation_id) as acquired:
        if not acquired:
            path = _operation_path(config, operation_id)
            return _public(config, read_json(path)) if path.is_file() else response(
                status="busy", workspace=str(config.config_path), operation_id=operation_id)
        return _show_operation_unlocked(config, operation_id)


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
    return _finish(config, path, record, package / source_raw, output, "localized", checked.get("receipt"),
                   checked.get("verification"))


def reconcile_operation(config: WorkspaceConfig, operation_id: str) -> dict[str, Any]:
    from .media_operations import _lock
    with _lock(config, operation_id) as acquired:
        if not acquired:
            path = _operation_path(config, operation_id)
            return _public(config, read_json(path)) if path.is_file() else show_operation(config, operation_id)
        return _reconcile_operation_unlocked(config, operation_id)
