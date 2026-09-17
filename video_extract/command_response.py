"""Shared command-response-v1 envelope and exit semantics."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


API_VERSION = 1
PROJECT = Path(__file__).resolve().parent.parent
PUBLIC_STATUSES = {
    "completed", "missing_input", "missing_dependency", "awaiting_model",
    "awaiting_user", "busy", "recoverable_failure", "uncertain",
    "unsupported", "failed",
}


def engineering_revision() -> str:
    completed = subprocess.run(
        ["git", "-C", str(PROJECT), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def workspace_id(path: str | None) -> str:
    if not path:
        raw = "unscoped"
    else:
        config = Path(path).expanduser()
        try:
            parsed = tomllib.loads(config.read_text(encoding="utf-8"))
            explicit = parsed.get("workspace_id")
            if isinstance(explicit, str) and explicit.strip():
                raw = "explicit:" + explicit.strip()
            else:
                raw = json.dumps(parsed, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            raw = "unresolved-workspace"
    return "workspace-" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def response(
    *, status: str, workspace: str | None = None, operation_id: str | None = None,
    result: Any = None, validation: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None, next_action: Any = None,
    diagnostics: list[str] | None = None, artifact_refs: list[str] | None = None,
) -> dict[str, Any]:
    if status not in PUBLIC_STATUSES:
        raise ValueError(f"invalid public status: {status}")
    return {
        "api_version": API_VERSION,
        "workspace_id": workspace_id(workspace),
        "operation_id": operation_id,
        "status": status,
        "observed_revision": engineering_revision(),
        "result": result,
        "artifact_refs": artifact_refs or [],
        "validation": validation or {},
        "provenance": provenance or {},
        "next_action": next_action,
        "diagnostics": diagnostics or [],
    }


def exit_code(value: dict[str, Any]) -> int:
    status = value.get("status")
    if status == "completed":
        return 0
    if status in {"missing_input", "missing_dependency", "awaiting_model", "awaiting_user", "busy", "uncertain"}:
        return 3
    return 1
