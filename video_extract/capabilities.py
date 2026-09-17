"""Stable capability IDs and command-response-v1 adaptation."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import json
import shutil
import typing
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .command_response import PUBLIC_STATUSES, engineering_revision, exit_code, response, workspace_id
from .manifest import read_json
from .workspace import WorkspaceError, discover_workspace


@dataclass(frozen=True)
class Capability:
    id: str
    contract_version: int
    implementation_version: int
    implementation: str
    input_type: str
    output_type: str
    side_effect: str
    dependencies: tuple[str, ...]
    authorization_category: str
    recovery_query: str


CAPABILITIES: dict[str, Capability] = {
    "audio.mandarin": Capability(
        id="audio.mandarin",
        contract_version=1,
        implementation_version=1,
        implementation="video_extract.mandarin_audio:run_mandarin_audio",
        input_type="mandarin-audio-request-v1",
        output_type="command-response-v1",
        side_effect="local_normalization_or_authorized_paid_tts",
        dependencies=("command:ffmpeg", "command:ffprobe"),
        authorization_category="local_workspace_or_paid_tts",
        recovery_query="operation show/resume/reconcile with the returned operation_id",
    ),
    "learning.learn": Capability(
        id="learning.learn",
        contract_version=1,
        implementation_version=1,
        implementation="video_extract.capabilities:run_learning",
        input_type="learning-request-v1",
        output_type="command-response-v1",
        side_effect="workspace_write",
        dependencies=(),
        authorization_category="local_workspace",
        recovery_query="capability run learning.learn with the same request",
    ),
    "source.notes": Capability(
        id="source.notes",
        contract_version=1,
        implementation_version=1,
        implementation="video_extract.capabilities:run_source_notes",
        input_type="source-notes-request-v1",
        output_type="command-response-v1",
        side_effect="workspace_write",
        dependencies=("command:ffmpeg", "command:ffprobe", "python:PIL", "python:faster_whisper"),
        authorization_category="local_workspace",
        recovery_query="capability run source.notes with the same request",
    ),
    "media.acquire": Capability(
        id="media.acquire",
        contract_version=1,
        implementation_version=1,
        implementation="video_extract.media_operations:run_media_acquire",
        input_type="media-acquire-request-v1",
        output_type="command-response-v1",
        side_effect="authorized_remote_read_and_workspace_write",
        dependencies=("command:ffmpeg", "command:ffprobe"),
        authorization_category="user_authorized_media_source",
        recovery_query="operation show/resume with the returned operation_id",
    ),
}


def _resolve(entry: Capability) -> Callable[[dict[str, Any]], dict[str, Any]] | None:
    module_name, separator, name = entry.implementation.partition(":")
    if not separator:
        return None
    try:
        value = getattr(importlib.import_module(module_name), name)
    except (ImportError, AttributeError):
        return None
    return value if callable(value) else None


def _operation_id(entry: Capability, request: dict[str, Any]) -> str:
    if entry.id == "audio.mandarin":
        try:
            from .mandarin_audio import operation_identity
            return operation_identity(request)
        except (KeyError, TypeError, ValueError, FileNotFoundError, WorkspaceError):
            pass
    # Authorization references prove permission for an already identified
    # operation; renewing or supplying one must not create a second external
    # request identity.
    logical_request = {key: value for key, value in request.items()
                       if key not in {"workspace", "package", "authorization_ref"}}
    package = request.get("package")
    if isinstance(package, str):
        manifest = Path(package) / "manifest.json"
        if manifest.is_file():
            try:
                logical_request["package_identity"] = read_json(manifest).get("identity")
            except (OSError, json.JSONDecodeError):
                pass
    stable = json.dumps({"capability": entry.id, "contract_version": entry.contract_version,
                         "workspace_id": workspace_id(request.get("workspace")),
                         "request": logical_request}, sort_keys=True, ensure_ascii=False)
    return "operation-" + hashlib.sha256(stable.encode()).hexdigest()[:24]


def _dependency_available(dependency: str) -> bool:
    kind, separator, name = dependency.partition(":")
    if not separator:
        return False
    if kind == "command":
        return shutil.which(name) is not None
    if kind == "python":
        return importlib.util.find_spec(name) is not None
    return False


def _contract_compatible(entry: Capability, implementation: Callable[..., Any] | None) -> bool:
    if implementation is None:
        return False
    declared = getattr(implementation, "__capability_contract__", None)
    if declared != {"input_type": entry.input_type, "output_type": entry.output_type}:
        return False
    try:
        signature = inspect.signature(implementation)
        signature.bind({})
    except (TypeError, ValueError):
        return False
    try:
        annotations = typing.get_type_hints(implementation)
    except (NameError, TypeError):
        annotations = {}
    first_parameter = next(iter(signature.parameters.values()), None)
    if first_parameter is None:
        return False
    input_annotation = annotations.get(first_parameter.name, first_parameter.annotation)
    output_annotation = annotations.get("return", signature.return_annotation)
    return _input_annotation_compatible(input_annotation) and _output_annotation_compatible(output_annotation)


def _input_annotation_compatible(annotation: Any) -> bool:
    if annotation in {inspect.Signature.empty, Any}:
        return True
    origin = typing.get_origin(annotation) or annotation
    try:
        return isinstance(origin, type) and issubclass(origin, Mapping) and issubclass(dict, origin)
    except TypeError:
        return False


def _output_annotation_compatible(annotation: Any) -> bool:
    if annotation in {inspect.Signature.empty, Any}:
        return True
    if typing.is_typeddict(annotation):
        return True
    origin = typing.get_origin(annotation) or annotation
    try:
        return isinstance(origin, type) and issubclass(origin, Mapping)
    except TypeError:
        return False


def list_capabilities() -> dict[str, Any]:
    items = [asdict(CAPABILITIES[key]) for key in sorted(CAPABILITIES)]
    for item in items:
        item["dependencies"] = list(item["dependencies"])
    return response(status="completed", result={"capabilities": items})


def check_capabilities(capability_id: str | None = None) -> dict[str, Any]:
    if capability_id and capability_id not in CAPABILITIES:
        return response(
            status="unsupported",
            validation={"capability_id": "failed"},
            diagnostics=[f"unknown capability id: {capability_id}"],
            next_action={"maintenance_path": str(Path(__file__).resolve())},
        )
    entries = [CAPABILITIES[capability_id]] if capability_id else [CAPABILITIES[key] for key in sorted(CAPABILITIES)]
    results = []
    diagnostics = []
    for entry in entries:
        available = _resolve(entry) is not None
        implementation = _resolve(entry)
        compatible = _contract_compatible(entry, implementation)
        dependencies = {dependency: _dependency_available(dependency) for dependency in entry.dependencies}
        contract_state = "unavailable" if not available else ("compatible" if compatible else "incompatible")
        results.append({**asdict(entry), "dependencies": list(entry.dependencies),
                        "implementation_state": "available" if available else "missing",
                        "contract_state": contract_state,
                        "dependency_state": dependencies})
        if not available:
            diagnostics.append(
                f"{entry.id} implementation is unavailable; update video_extract.capabilities:CAPABILITIES"
            )
        for dependency, present in dependencies.items():
            if not present:
                diagnostics.append(f"{entry.id} dependency is unavailable: {dependency}")
        if available and not compatible:
            diagnostics.append(f"{entry.id} input/output contract or callable signature is incompatible")
    dependencies_ok = all(all(item["dependency_state"].values()) for item in results)
    implementations_ok = all(item["implementation_state"] == "available" for item in results)
    contract_failed = any(item["contract_state"] == "incompatible" for item in results)
    contracts_checked = all(item["contract_state"] != "unavailable" for item in results)
    if contract_failed:
        status = "unsupported"
    else:
        status = "completed" if not diagnostics else "missing_dependency"
    return response(status=status, result={"capabilities": results},
                     validation={"implementations": "passed" if implementations_ok else "failed",
                                 "dependencies": "passed" if dependencies_ok else "failed",
                                 "contracts": "failed" if contract_failed else ("passed" if contracts_checked else "not_checked")},
                     diagnostics=diagnostics)


def run_source_notes(request: dict[str, Any]) -> dict[str, Any]:
    from .authoritative_notes import finalize_note, prepare_note
    from .notes_workflow import finalize, prepare

    workspace = discover_workspace(Path(request["workspace"]))
    if not workspace.workspace_id:
        raise WorkspaceError("workspace_id is required for stable public capability execution; add a persistent logical ID to workspace.toml")
    action = request.get("action")
    if workspace.schema_version == 2:
        if action == "prepare":
            prepared = prepare_note(workspace, request["source_id"], request.get("source_version"))
            return {"status": "awaiting_ai", "action": "notes_write",
                    "input": prepared["result"]["model_input"], "source_id": request["source_id"]}
        if action == "finalize":
            finalized = finalize_note(workspace, Path(request["request"]))
            return {"status": "complete" if finalized["status"] == "completed" else finalized["status"],
                    "output": (finalized.get("result") or {}).get("note"), "details": finalized}
        return {"status": "needs_input", "error": "action must be prepare or finalize"}
    package = Path(request["package"])
    if action == "prepare":
        return prepare(package, workspace)
    if action == "finalize":
        return finalize(package, workspace)
    return {"status": "needs_input", "error": "action must be prepare or finalize"}


run_source_notes.__capability_contract__ = {
    "input_type": "source-notes-request-v1",
    "output_type": "command-response-v1",
}


def run_learning(request: dict[str, Any]) -> dict[str, Any]:
    from .learning import (LearningPublishError, back, commit_explanation, create_module, create_thread,
                           locate, prepare_explanation, publish_failure_response, pursue,
                           recommend_roots, record_feedback, resume, show_module, show_thread)

    workspace = discover_workspace(Path(request["workspace"]))
    action = request.get("action")
    try:
        if action == "module.create":
            return create_module(workspace, request["goal"], request["scope"], request["source_id"],
                                 request["source_version"], request.get("expected_revision"),
                                 request.get("source_role"))
        if action == "module.show": return show_module(workspace, request["module_id"])
        if action == "recommend": return recommend_roots(workspace, request["module_id"])
        if action == "thread.create":
            return create_thread(workspace, request["module_id"], request["root_question"],
                                 request.get("expected_revision"))
        if action == "thread.show": return show_thread(workspace, request["thread_id"])
        if action == "pursue":
            return pursue(workspace, request["thread_id"], request["from_question_id"], request["relation"],
                          request.get("question"), request.get("expected_revision"),
                          request.get("existing_question_id"), bool(request.get("independent", False)),
                          request.get("actual_question"))
        if action == "feedback":
            return record_feedback(workspace, request["question_id"], request["state"], request["text"],
                                   request.get("confusion"), request.get("expected_revision"))
        if action == "resume":
            return resume(workspace, request.get("thread_id"), request.get("question_id"),
                          request.get("from_question_id"), request.get("expected_revision"),
                          request.get("module_id"))
        if action == "back":
            return back(workspace, request["thread_id"], request.get("expected_revision"))
        if action == "locate": return locate(workspace, request["question_id"])
        if action == "explanation.prepare":
            return prepare_explanation(workspace, request["question_id"], request["profile"])
        if action == "explanation.commit":
            return commit_explanation(workspace, request["question_id"], Path(request["draft"]),
                                      Path(request["evidence"]), Path(request["teaching_review"]),
                                      request["profile"], request["preparation_id"],
                                      request.get("expected_revision"))
        return {"status": "needs_input", "error": "unsupported learning action"}
    except LearningPublishError as exc:
        return publish_failure_response(workspace, exc)


run_learning.__capability_contract__ = {
    "input_type": "learning-request-v1",
    "output_type": "command-response-v1",
}


def run_capability(capability_id: str, request_path: Path) -> dict[str, Any]:
    entry = CAPABILITIES.get(capability_id)
    if entry is None:
        return response(status="unsupported", validation={"capability_id": "failed"},
                         diagnostics=[f"unknown capability id: {capability_id}"],
                         next_action={"maintenance_path": str(Path(__file__).resolve())})
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return response(status="missing_input", validation={"request": "failed"}, diagnostics=[str(exc)])
    if not isinstance(request, dict):
        return response(status="missing_input", validation={"request": "failed"},
                         diagnostics=["request must be a JSON object"])
    workspace = request.get("workspace")
    operation_id = _operation_id(entry, request)
    provenance = {"capability_id": capability_id, "implementation": entry.implementation,
                  "implementation_version": entry.implementation_version,
                  "source_version": request.get("source_version"), "engineering_revision": engineering_revision()}
    if request.get("contract_version") != entry.contract_version:
        return response(
            status="unsupported", workspace=workspace, operation_id=operation_id,
            validation={"contract_version": "failed"}, provenance=provenance,
            diagnostics=[f"{capability_id} requires contract_version {entry.contract_version}"],
            next_action={"type": "maintenance", "maintenance_path": str(Path(__file__).resolve())},
        )
    implementation = _resolve(entry)
    if implementation is None:
        return response(status="missing_dependency", workspace=workspace, operation_id=operation_id,
                         validation={"implementation": "failed"}, provenance=provenance,
                         diagnostics=[f"{entry.implementation} is unavailable"],
                         next_action={"type": "maintenance", "maintenance_path": str(Path(__file__).resolve())})
    if not _contract_compatible(entry, implementation):
        return response(status="unsupported", workspace=workspace, operation_id=operation_id,
                        validation={"input_type": "failed", "output_type": "failed"}, provenance=provenance,
                        diagnostics=[f"{capability_id} implementation does not satisfy its declared contract"],
                        next_action={"type": "maintenance", "maintenance_path": str(Path(__file__).resolve())})
    try:
        raw = implementation(request)
    except (KeyError, TypeError, ValueError, FileNotFoundError, WorkspaceError) as exc:
        return response(status="missing_input", workspace=workspace, operation_id=operation_id,
                         validation={"request": "failed", "contract_version": "passed"},
                         provenance=provenance, diagnostics=[str(exc)],
                         next_action={"type": "user", "reason": "correct the request"})
    if not isinstance(raw, Mapping):
        return response(status="failed", workspace=workspace, operation_id=operation_id,
                        validation={"contract_version": "passed", "output_type": "failed"},
                        provenance=provenance, diagnostics=["capability implementation returned a non-mapping result"],
                        next_action={"type": "maintenance", "maintenance_path": str(Path(__file__).resolve())})
    if raw.get("api_version") == 1:
        # Implementations of command-response-v1 may provide the complete
        # public envelope (including durable progress and artifact references).
        envelope = dict(raw)
        envelope["operation_id"] = operation_id
        envelope["provenance"] = {**provenance, **(raw.get("provenance") or {})}
        return envelope
    status_map = {"complete": "completed", "ready": "completed", "awaiting_ai": "awaiting_model",
                  "needs_input": "missing_input", "failed": "failed"}
    status = status_map.get(raw.get("status"), raw.get("status"))
    if status not in PUBLIC_STATUSES:
        status = "failed"
    next_action = None
    if status == "awaiting_model":
        next_action = {"type": "model", "action": raw.get("action"), "resume": raw.get("resume")}
    elif status == "missing_input":
        next_action = {"type": "user", "reason": raw.get("error")}
    artifacts = [value for key, value in raw.items() if key in {"package", "note", "output"} and value]
    return response(status=status, workspace=workspace, operation_id=operation_id, result=raw,
                    validation={"contract_version": "passed"}, provenance=provenance,
                    next_action=next_action, artifact_refs=artifacts,
                    diagnostics=[] if status not in {"failed", "missing_input"} else [str(raw.get("error") or "capability failed")])
