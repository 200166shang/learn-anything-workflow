"""Stable capability IDs and command-response-v1 adaptation."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import json
import shutil
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .command_response import PUBLIC_STATUSES, engineering_revision, exit_code, response
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
    logical_request = {key: value for key, value in request.items() if key not in {"workspace", "package"}}
    package = request.get("package")
    if isinstance(package, str):
        manifest = Path(package) / "manifest.json"
        if manifest.is_file():
            try:
                logical_request["package_identity"] = read_json(manifest).get("identity")
            except (OSError, json.JSONDecodeError):
                pass
    stable = json.dumps({"capability": entry.id, "contract_version": entry.contract_version,
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
    if entry.input_type != "source-notes-request-v1" or entry.output_type != "command-response-v1":
        return False
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
    return True


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
        results.append({**asdict(entry), "dependencies": list(entry.dependencies),
                        "implementation_state": "available" if available else "missing",
                        "contract_state": "compatible" if compatible else "incompatible",
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
    contracts_ok = all(item["contract_state"] == "compatible" for item in results)
    if not contracts_ok and dependencies_ok and implementations_ok:
        status = "unsupported"
    else:
        status = "completed" if not diagnostics else "missing_dependency"
    return response(status=status, result={"capabilities": results},
                     validation={"implementations": "passed" if implementations_ok else "failed",
                                 "dependencies": "passed" if dependencies_ok else "failed",
                                 "contracts": "passed" if contracts_ok else "failed"},
                     diagnostics=diagnostics)


def run_source_notes(request: dict[str, Any]) -> dict[str, Any]:
    from .notes_workflow import finalize, prepare

    workspace = discover_workspace(Path(request["workspace"]))
    if not workspace.workspace_id:
        raise WorkspaceError("workspace_id is required for stable public capability execution; add a persistent logical ID to workspace.toml")
    package = Path(request["package"])
    action = request.get("action")
    if action == "prepare":
        return prepare(package, workspace)
    if action == "finalize":
        return finalize(package, workspace)
    return {"status": "needs_input", "error": "action must be prepare or finalize"}


run_source_notes.__capability_contract__ = {
    "input_type": "source-notes-request-v1",
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
