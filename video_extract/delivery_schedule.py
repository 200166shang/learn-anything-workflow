"""User-level macOS scheduling for the public delivery CLI."""

from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .command_response import response
from .manifest import atomic_write_json, read_json
from .workspace import PORTABLE_SCHEMA_VERSION, WorkspaceConfig, WorkspaceError

LABEL_PREFIX = "com.video-extract.delivery"
Run = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _host_fingerprint(hostname: str | None = None) -> str:
    return hashlib.sha256((hostname or socket.gethostname()).encode()).hexdigest()[:16]


def _paths(config: WorkspaceConfig, home: Path | None = None) -> tuple[str, Path, Path, Path]:
    if config.schema_version != PORTABLE_SCHEMA_VERSION or config.local is None or not config.workspace_id:
        raise WorkspaceError("delivery scheduling requires workspace schema v2")
    suffix = hashlib.sha256(config.workspace_id.encode()).hexdigest()[:12]
    label = f"{LABEL_PREFIX}.{suffix}"
    local_root = config.local / "delivery"
    agents = (home or Path.home()) / "Library" / "LaunchAgents"
    return label, agents / f"{label}.plist", local_root / "scheduler-host.json", local_root / "logs"


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True)


def _resolve_executable(executable: Path | None = None) -> Path | None:
    if executable is not None:
        candidate = executable.expanduser().resolve()
    else:
        invoked = Path(sys.argv[0]).expanduser()
        if invoked.name == "video-extract" and invoked.is_file():
            candidate = invoked.resolve()
        else:
            found = shutil.which("video-extract")
            candidate = Path(found).resolve() if found else None
    return candidate if candidate and candidate.is_file() and os.access(candidate, os.X_OK) else None


def _plist(config: WorkspaceConfig, label: str, executable: Path, logs: Path,
           environment: Mapping[str, str]) -> dict[str, Any]:
    physical_target = environment.get("VIDEO_EXTRACT_LARK_CHAT_ID", "").strip()
    if not physical_target:
        raise ValueError("VIDEO_EXTRACT_LARK_CHAT_ID is required for scheduler installation")
    search_path = environment.get("PATH", os.defpath)
    lark_cli = shutil.which("lark-cli", path=search_path)
    runtime_directories = [str(executable.parent)]
    if lark_cli:
        runtime_directories.append(str(Path(lark_cli).resolve().parent))
    runtime_directories.extend(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                                "/usr/sbin", "/sbin"])
    runtime_path = ":".join(dict.fromkeys(runtime_directories))
    return {
        "Label": label,
        "ProgramArguments": [str(executable), "delivery", "tick", "--workspace",
                             str(config.config_path), "--json"],
        "RunAtLoad": True,
        # An empty calendar dictionary means every minute. launchd coalesces missed
        # calendar events after wake; the CLI decides whether today's work is due.
        "StartCalendarInterval": [{}],
        "EnvironmentVariables": {"VIDEO_EXTRACT_LARK_CHAT_ID": physical_target,
                                 "PATH": runtime_path},
        "StandardOutPath": str(logs / "stdout.log"),
        "StandardErrorPath": str(logs / "stderr.log"),
        "ProcessType": "Background",
    }


def _verified_delivery_configuration(config: WorkspaceConfig,
                                     environment: Mapping[str, str]) -> tuple[bool, str]:
    assert config.local is not None
    path = config.local / "delivery" / "config.json"
    if not path.is_file():
        return False, "delivery must be enabled before installing its scheduler"
    configuration = read_json(path)
    if not configuration.get("enabled"):
        return False, "delivery is disabled"
    target = environment.get("VIDEO_EXTRACT_LARK_CHAT_ID", "").strip()
    if not target:
        return False, "VIDEO_EXTRACT_LARK_CHAT_ID is required for scheduler installation"
    fingerprint = hashlib.sha256(target.encode()).hexdigest()[:16]
    expected = configuration.get("readiness", {}).get("target_fingerprint")
    if fingerprint != expected:
        return False, "physical recipient differs from the authorized delivery target"
    return True, ""


def _write_plist(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(config: WorkspaceConfig, *, home: Path | None = None,
            environment: Mapping[str, str] | None = None, executable: Path | None = None,
            runner: Run = _run, uid: int | None = None, hostname: str | None = None,
            clock: Callable[[], datetime] = datetime.now) -> dict[str, Any]:
    label, plist_path, marker_path, logs = _paths(config, home)
    fingerprint = _host_fingerprint(hostname)
    if marker_path.is_file():
        marker = read_json(marker_path)
        if marker.get("host_fingerprint") != fingerprint:
            return response(status="awaiting_user", workspace=str(config.config_path),
                            diagnostics=["another host owns this workspace scheduler; disable it there before migration"],
                            next_action={"type": "user", "action": "disable old scheduler host"})
    selected_environment = environment or os.environ
    ready, diagnostic = _verified_delivery_configuration(config, selected_environment)
    if not ready:
        return response(status="awaiting_user", workspace=str(config.config_path),
                        diagnostics=[diagnostic],
                        next_action={"type": "user", "action": "delivery enable"})
    resolved = _resolve_executable(executable)
    if resolved is None:
        return response(status="missing_dependency", workspace=str(config.config_path),
                        diagnostics=["installed video-extract executable not found"])
    logs.mkdir(parents=True, exist_ok=True)
    value = _plist(config, label, resolved, logs, selected_environment)
    _write_plist(plist_path, value)
    domain = f"gui/{uid if uid is not None else os.getuid()}"
    # Replace only this workspace's exact label. A missing prior job is harmless.
    runner(["launchctl", "bootout", f"{domain}/{label}"])
    loaded = runner(["launchctl", "bootstrap", domain, str(plist_path)])
    if loaded.returncode != 0:
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        diagnostics=[loaded.stderr.strip() or "launchctl bootstrap failed"],
                        next_action={"type": "retry", "action": "delivery schedule-install"})
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker = {"schema_version": 1, "workspace_id": config.workspace_id, "label": label,
              "host_fingerprint": fingerprint, "plist_path": str(plist_path),
              "executable": str(resolved), "enabled": True, "installed_at": clock().isoformat()}
    atomic_write_json(marker_path, marker)
    return response(status="completed", workspace=str(config.config_path),
                    result={"installed": True, "enabled": True, "label": label,
                            "interval_seconds": 60, "run_at_load": True},
                    validation={"unique_host": "passed", "public_cli_only": "passed"})


def remove(config: WorkspaceConfig, *, home: Path | None = None, runner: Run = _run,
           uid: int | None = None, hostname: str | None = None,
           clock: Callable[[], datetime] = datetime.now) -> dict[str, Any]:
    label, plist_path, marker_path, _ = _paths(config, home)
    fingerprint = _host_fingerprint(hostname)
    if marker_path.is_file():
        marker = read_json(marker_path)
        if marker.get("host_fingerprint") != fingerprint:
            return response(status="awaiting_user", workspace=str(config.config_path),
                            diagnostics=["scheduler marker belongs to another host"])
    domain = f"gui/{uid if uid is not None else os.getuid()}"
    runner(["launchctl", "bootout", f"{domain}/{label}"])
    if plist_path.is_file():
        plist_path.unlink()
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(marker_path, {"schema_version": 1, "workspace_id": config.workspace_id,
                                    "label": label, "host_fingerprint": fingerprint,
                                    "enabled": False, "disabled_at": clock().isoformat()})
    return response(status="completed", workspace=str(config.config_path),
                    result={"installed": False, "enabled": False, "label": label})


def status(config: WorkspaceConfig, *, home: Path | None = None, runner: Run = _run,
           uid: int | None = None, hostname: str | None = None) -> dict[str, Any]:
    label, plist_path, marker_path, _ = _paths(config, home)
    fingerprint = _host_fingerprint(hostname)
    marker = read_json(marker_path) if marker_path.is_file() else None
    owner_matches = bool(marker and marker.get("host_fingerprint") == fingerprint)
    enabled = bool(marker and marker.get("enabled"))
    plist_valid = False
    if plist_path.is_file():
        try:
            value = plistlib.loads(plist_path.read_bytes())
            arguments = value.get("ProgramArguments", [])
            plist_valid = (value.get("Label") == label and value.get("RunAtLoad") is True
                           and value.get("StartCalendarInterval") == [{}]
                           and arguments[1:3] == ["delivery", "tick"]
                           and str(config.config_path) in arguments)
        except (OSError, ValueError, plistlib.InvalidFileException):
            pass
    domain = f"gui/{uid if uid is not None else os.getuid()}"
    probe = runner(["launchctl", "print", f"{domain}/{label}"])
    loaded = probe.returncode == 0
    healthy = enabled and owner_matches and plist_valid and loaded
    public_status = "completed" if healthy or not marker else "awaiting_user"
    diagnostics = [] if healthy else (["scheduler is paused on this host"] if not enabled else
                                      ["scheduler installation requires repair or host verification"])
    return response(status=public_status, workspace=str(config.config_path),
                    result={"installed": plist_path.is_file(), "enabled": enabled, "loaded": loaded,
                            "owner_matches": owner_matches, "configuration_valid": plist_valid,
                            "label": label}, diagnostics=diagnostics,
                    validation={"unique_host": "passed" if owner_matches else "not_verified"})
