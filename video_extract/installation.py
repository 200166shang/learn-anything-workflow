"""Install project-owned Skills and Agents without creating a second source."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .command_response import response


PROJECT = Path(__file__).resolve().parent.parent
CONTRACT_VERSION = 4
API_VERSION = 1
RECEIPT_PARTS = ("video-extract", "install-receipt.json")


@dataclass(frozen=True)
class InstallEntry:
    id: str
    source: Path
    target_parts: tuple[str, ...]
    host: str

    def target(self, agents_root: Path, codex_root: Path, obsidian_plugins_root: Path | None = None) -> Path:
        root = agents_root if self.host == "agents" else codex_root if self.host == "codex" else obsidian_plugins_root
        if root is None:
            raise ValueError("Obsidian plugin root is not configured")
        return root.joinpath(*self.target_parts)


@dataclass(frozen=True)
class RetiredEntry:
    id: str
    target_parts: tuple[str, ...]
    host: str

    def target(self, agents_root: Path, codex_root: Path) -> Path:
        root = agents_root if self.host == "agents" else codex_root
        return root.joinpath(*self.target_parts)


ENTRIES = (
    InstallEntry("agent.mandarin-netease", PROJECT / ".codex/agents/mandarin-netease-operator.toml", ("agents", "mandarin-netease-operator.toml"), "codex"),
    InstallEntry("agent.source-notes", PROJECT / ".codex/agents/source-notes-operator.toml", ("agents", "source-notes-operator.toml"), "codex"),
    InstallEntry("skill.extract-media", PROJECT / "integrations/skills/extract-media", ("skills", "extract-media"), "agents"),
    InstallEntry("skill.learn-anything", PROJECT / "integrations/skills/learn-anything", ("skills", "learn-anything"), "agents"),
    InstallEntry("skill.learning", PROJECT / "integrations/skills/learning", ("skills", "learning"), "agents"),
    InstallEntry("skill.mandarin-audio", PROJECT / "integrations/skills/mandarin-audio", ("skills", "mandarin-audio"), "agents"),
    InstallEntry("skill.practice", PROJECT / "integrations/skills/practice", ("skills", "practice"), "agents"),
    InstallEntry("skill.source-notes", PROJECT / "integrations/skills/source-notes", ("skills", "source-notes"), "agents"),
    InstallEntry("skill.review", PROJECT / "integrations/skills/review", ("skills", "review"), "agents"),
)

OBSIDIAN_ENTRY = InstallEntry("plugin.obsidian-learning-map",
                              PROJECT / "integrations/obsidian-learning-map",
                              ("video-extract-learning-map",), "obsidian")

# These are superseded runtime entries, not data formats.  `apply` moves an
# installed copy out of the host's discovery root and into the transaction
# backup.  It never deletes the copy or creates a forwarding alias.
RETIRED_ENTRIES = (
    RetiredEntry("legacy.skill.video-learning", ("skills", "video-learning"), "agents"),
    RetiredEntry("legacy.skill.video-learning-workspace", ("skills", "video-learning-workspace"), "agents"),
    RetiredEntry("legacy.skill.xiaoe-video-learning-workspace", ("skills", "xiaoe-video-learning-workspace"), "agents"),
    RetiredEntry("legacy.skill.learning-router", ("skills", "learning"), "codex"),
    RetiredEntry("legacy.skill.learning-learn", ("skills", "learning-learn"), "codex"),
    RetiredEntry("legacy.skill.learning-review", ("skills", "learning-review"), "codex"),
    RetiredEntry("legacy.skill.learning-practice", ("skills", "learning-practice"), "codex"),
)


def _files(path: Path) -> list[Path]:
    return [path] if path.is_file() else sorted(item for item in path.rglob("*") if item.is_file())


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in _files(path):
        digest.update(str(item.relative_to(path) if path.is_dir() else item.name).encode())
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _revision() -> str:
    completed = subprocess.run(
        ["git", "-C", str(PROJECT), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def source_info() -> dict[str, Any]:
    digest = hashlib.sha256()
    roots = [PROJECT / "pyproject.toml", PROJECT / "video_extract", PROJECT / "schemas",
             PROJECT / "integrations/skills", PROJECT / ".codex/agents"]
    files = sorted({item for root in roots for item in _files(root)
                    if "__pycache__" not in item.parts and item.suffix != ".pyc"})
    fingerprinted_paths = []
    for item in files:
        relative = str(item.relative_to(PROJECT))
        fingerprinted_paths.append(relative)
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    status = subprocess.run(
        ["git", "-C", str(PROJECT), "status", "--porcelain"], capture_output=True, text=True
    )
    return {
        "path": str(PROJECT),
        "revision": _revision(),
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
        "content_fingerprint": digest.hexdigest(),
        "fingerprinted_paths": fingerprinted_paths,
        "contract_version": CONTRACT_VERSION,
    }


def _entry_state(entry: InstallEntry, target: Path) -> str:
    if not target.exists() and not target.is_symlink():
        return "missing"
    if target.is_symlink() and target.resolve() == entry.source.resolve():
        return "linked"
    if target.exists() and _digest(target) == _digest(entry.source):
        return "copied"
    return "drifted"


def _dependencies() -> dict[str, bool]:
    checks = {name: bool(shutil.which(name)) for name in ("ffmpeg", "ffprobe", "yt-dlp")}
    for module, label in (("PIL", "pillow"), ("playwright", "playwright")):
        try:
            __import__(module)
            checks[label] = True
        except ImportError:
            checks[label] = False
    return checks


def _tool() -> dict[str, Any]:
    executable = shutil.which("video-extract")
    observed_source = None
    if executable:
        completed = subprocess.run([executable, "doctor", "--json"], capture_output=True, text=True)
        if completed.returncode in {0, 1}:
            try:
                observed_source = json.loads(completed.stdout).get("project")
            except json.JSONDecodeError:
                pass
    try:
        version = importlib.metadata.version("video-extract")
    except importlib.metadata.PackageNotFoundError:
        version = None
    return {
        "executable": executable,
        "source": observed_source,
        "matches_source": observed_source == str(PROJECT),
        "python": sys.executable,
        "version": version,
    }


def _inspect_details(agents_root: Path, codex_root: Path,
                     obsidian_plugins_root: Path | None = None) -> tuple[str, dict[str, Any], list[str]]:
    entries = []
    for entry in ENTRIES:
        target = entry.target(agents_root, codex_root)
        entries.append(
            {
                "id": entry.id,
                "state": _entry_state(entry, target),
                "target": str(target),
                "maintenance_path": str(entry.source),
                "content_fingerprint": _digest(entry.source),
            }
        )
    dependencies = _dependencies()
    plugin_target = (OBSIDIAN_ENTRY.target(agents_root, codex_root, obsidian_plugins_root)
                     if obsidian_plugins_root is not None else None)
    plugin = {
        "id": OBSIDIAN_ENTRY.id,
        "state": _entry_state(OBSIDIAN_ENTRY, plugin_target) if plugin_target is not None else "not_configured",
        "target": str(plugin_target) if plugin_target is not None else None,
        "maintenance_path": str(OBSIDIAN_ENTRY.source),
        "content_fingerprint": _digest(OBSIDIAN_ENTRY.source),
        "required_when_configured": True,
    }
    retired_entries = []
    for entry in RETIRED_ENTRIES:
        target = entry.target(agents_root, codex_root)
        active = target.exists() or target.is_symlink()
        retired_entries.append({
            "id": entry.id,
            "state": "active" if active else "retired",
            "target": str(target),
            "policy": "move_to_read_only_install_backup",
        })
    tool = _tool()
    entries_ok = all(item["state"] == "linked" for item in entries)
    retirement_ok = all(item["state"] == "retired" for item in retired_entries)
    plugin_ok = plugin["state"] == "linked"
    dependencies_ok = all(dependencies.values())
    tool_ok = tool["matches_source"]
    receipt_path = codex_root.joinpath(*RECEIPT_PARTS)
    receipt = None
    if receipt_path.is_file():
        try:
            recorded = json.loads(receipt_path.read_text(encoding="utf-8"))
            recorded_source = recorded.get("source", {})
            current_source = source_info()
            if recorded_source.get("contract_version") != CONTRACT_VERSION:
                state = "contract_drift"
            elif recorded_source.get("revision") != current_source["revision"]:
                state = "revision_drift"
            elif recorded_source.get("content_fingerprint") != current_source["content_fingerprint"]:
                state = "source_drift"
            else:
                state = "matched"
            receipt = {"path": str(receipt_path), "state": state, "recorded_source": recorded_source}
        except (OSError, json.JSONDecodeError):
            receipt = {"path": str(receipt_path), "state": "invalid"}
    else:
        receipt = {"path": str(receipt_path), "state": "missing"}
    receipt_ok = receipt["state"] == "matched"
    ok = entries_ok and retirement_ok and plugin_ok and dependencies_ok and tool_ok and receipt_ok
    status = "completed"
    diagnostics = []
    if plugin["state"] == "not_configured":
        status = "missing_input"
        diagnostics.append("obsidian_plugins_root is required to publish and verify the project-owned navigation plugin")
    elif not entries_ok or not retirement_ok or not plugin_ok or not tool_ok:
        status = "recoverable_failure"
        diagnostics.append("source_or_install_drift: installed entries, retired entries, or the video-extract executable do not match the maintenance source")
    elif not receipt_ok:
        status = "recoverable_failure"
        diagnostics.append(f"source_or_install_drift: install receipt is {receipt['state']}")
    elif not dependencies_ok:
        status = "missing_dependency"
        diagnostics.append("one or more required deterministic dependencies are unavailable")
    details = {
        "ok": ok,
        "source": source_info(),
        "tool": tool,
        "dependencies": dependencies,
        "entries": entries,
        "retired_entries": retired_entries,
        "plugin": plugin,
        "receipt": receipt,
        "validation": {"entries": entries_ok, "retired_entries": retirement_ok, "plugin": plugin_ok,
                       "dependencies": dependencies_ok, "tool": tool_ok, "receipt": receipt_ok},
    }
    return status, details, diagnostics


def _wrap(status: str, details: dict[str, Any], diagnostics: list[str], *, operation_id: str | None = None,
          next_command: str | None = None) -> dict[str, Any]:
    next_action = None
    if next_command:
        next_action = {"command": next_command, "maintenance_path": str(PROJECT / "video_extract/installation.py")}
    return response(status=status, operation_id=operation_id, result=details,
                    validation=details.get("validation", {}),
                    provenance={"engineering_source": str(PROJECT),
                                "contract_version": CONTRACT_VERSION},
                    next_action=next_action, diagnostics=diagnostics,
                    artifact_refs=[details["backup"]] if details.get("backup") else [])


def _install_operation_id(agents_root: Path, codex_root: Path,
                          obsidian_plugins_root: Path | None = None) -> str:
    payload = json.dumps({"agents_root": str(agents_root.expanduser().resolve()),
                          "codex_root": str(codex_root.expanduser().resolve()),
                          "obsidian_plugins_root": str(obsidian_plugins_root.expanduser().resolve()) if obsidian_plugins_root else None,
                          "contract_version": CONTRACT_VERSION}, sort_keys=True)
    return "install-" + hashlib.sha256(payload.encode()).hexdigest()[:24]


def inspect(agents_root: Path, codex_root: Path, obsidian_plugins_root: Path | None = None) -> dict[str, Any]:
    status, details, diagnostics = _inspect_details(agents_root, codex_root, obsidian_plugins_root)
    return _wrap(status, details, diagnostics,
                 next_command=None if status == "completed" else "video-extract install plan --json")


def plan(agents_root: Path, codex_root: Path, obsidian_plugins_root: Path | None = None) -> dict[str, Any]:
    inspected_status, details, diagnostics = _inspect_details(agents_root, codex_root, obsidian_plugins_root)
    details["changes"] = [
        {"id": item["id"], "action": "keep" if item["state"] == "linked" else "link", "target": item["target"]}
        for item in details["entries"]
    ]
    details["changes"].extend(
        {"id": item["id"], "action": "keep_retired" if item["state"] == "retired" else "retire_to_backup",
         "target": item["target"]}
        for item in details["retired_entries"]
    )
    details["changes"].append({"id": OBSIDIAN_ENTRY.id,
                               "action": "not_configured" if details["plugin"]["state"] == "not_configured"
                               else "keep" if details["plugin"]["state"] == "linked" else "link",
                               "target": details["plugin"]["target"]})
    return _wrap("completed" if obsidian_plugins_root is not None else inspected_status,
                 details, diagnostics,
                 next_command="video-extract install apply --obsidian-plugins-root VAULT/.obsidian/plugins --json")


def apply(agents_root: Path, codex_root: Path, backup_root: Path | None = None,
          obsidian_plugins_root: Path | None = None) -> dict[str, Any]:
    if obsidian_plugins_root is None:
        status, details, diagnostics = _inspect_details(agents_root, codex_root, None)
        return _wrap(status, details, diagnostics,
                     operation_id=_install_operation_id(agents_root, codex_root, None),
                     next_command="video-extract install apply --obsidian-plugins-root VAULT/.obsidian/plugins --json")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = (backup_root or codex_root / "backups/video-extract-install") / stamp
    backed_up: list[dict[str, str]] = []
    changed: list[tuple[Path, Path | None]] = []
    try:
        for entry in ENTRIES:
            if not entry.source.exists():
                raise FileNotFoundError(f"maintenance source is missing: {entry.source}")
            target = entry.target(agents_root, codex_root)
            state = _entry_state(entry, target)
            if state == "linked":
                continue
            destination = None
            if target.exists() or target.is_symlink():
                relative = Path(entry.host) / Path(*entry.target_parts)
                destination = backup / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, destination)
                backed_up.append({"target": str(target), "backup": str(destination), "previous_state": state})
            changed.append((target, destination))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(entry.source, target_is_directory=entry.source.is_dir())
        if obsidian_plugins_root is not None:
            entry = OBSIDIAN_ENTRY
            target = entry.target(agents_root, codex_root, obsidian_plugins_root)
            state = _entry_state(entry, target)
            if state != "linked":
                destination = None
                if target.exists() or target.is_symlink():
                    destination = backup / "obsidian" / target.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, destination)
                    backed_up.append({"target": str(target), "backup": str(destination),
                                      "previous_state": state})
                changed.append((target, destination))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(entry.source, target_is_directory=True)
        for entry in RETIRED_ENTRIES:
            target = entry.target(agents_root, codex_root)
            if not target.exists() and not target.is_symlink():
                continue
            relative = Path(entry.host) / Path(*entry.target_parts)
            destination = backup / "retired" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, destination)
            backed_up.append({"target": str(target), "backup": str(destination),
                              "previous_state": "active_legacy_entry"})
            changed.append((target, destination))
        from .manifest import atomic_write_json
        receipt = codex_root.joinpath(*RECEIPT_PARTS)
        atomic_write_json(receipt, {"api_version": API_VERSION, "source": source_info(),
                                   "entries": [entry.id for entry in ENTRIES],
                                   "retired_entries": [entry.id for entry in RETIRED_ENTRIES],
                                   "plugin": OBSIDIAN_ENTRY.id if obsidian_plugins_root is not None else None,
                                   "retirement_policy": "preserved_outside_host_discovery"})
    except Exception as exc:
        rollback_errors = []
        for target, destination in reversed(changed):
            try:
                if target.is_symlink():
                    target.unlink()
                if destination is not None and (destination.exists() or destination.is_symlink()):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, target)
            except OSError as rollback_exc:
                rollback_errors.append(f"rollback failed for {target}: {rollback_exc}")
        _, details, _ = _inspect_details(agents_root, codex_root, obsidian_plugins_root)
        details.update({"ok": False, "backup": str(backup), "backed_up": backed_up})
        return _wrap("recoverable_failure", details, [str(exc), *rollback_errors],
                     operation_id=_install_operation_id(agents_root, codex_root, obsidian_plugins_root),
                     next_command="video-extract install plan --json")
    status, details, diagnostics = _inspect_details(agents_root, codex_root, obsidian_plugins_root)
    details.update({"backup": str(backup), "backed_up": backed_up})
    return _wrap(status, details, diagnostics,
                 operation_id=_install_operation_id(agents_root, codex_root, obsidian_plugins_root),
                 next_command=None if status == "completed" else "video-extract install plan --json")
