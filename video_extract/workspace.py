"""Portable workspace discovery, validation, auditing, and rebuild orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


SCHEMA_VERSION = 1
PORTABLE_SCHEMA_VERSION = 2
CONFIG_NAME = "workspace.toml"
LOCATOR = Path("~/.config/video-extract/config.toml").expanduser()
MANAGED_MARKER = ".video-extract-managed.json"
PROTECTED_VAULT_NAMES = {"threads", "concepts", "REVIEW.md", "收件箱"}


class WorkspaceError(RuntimeError):
    pass


def validate_workspace_id(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    return None if candidate == "REPLACE_ME_WITH_UUID" else candidate


def _contained(child: Path, parent: Path, label: str) -> Path:
    child, parent = child.resolve(), parent.resolve()
    try:
        child.relative_to(parent)
    except ValueError as exc:
        raise WorkspaceError(f"{label} escapes {parent}: {child}") from exc
    return child


@dataclass(frozen=True)
class WorkspaceConfig:
    config_path: Path
    source: str
    root: Path
    project: Path
    media: Path
    obsidian: Path
    generated: Path
    threads: Path
    concepts: Path
    review: Path
    workspace_id: str | None = None
    pyvideotrans_python: Path | None = None
    pyvideotrans_cli: Path | None = None
    schema_version: int = SCHEMA_VERSION
    results: Path | None = None
    sources: Path | None = None
    derived: Path | None = None
    local: Path | None = None

    @classmethod
    def load(cls, path: Path, source: str = "explicit") -> "WorkspaceConfig":
        path = path.expanduser().resolve()
        if not path.is_file():
            raise WorkspaceError(f"workspace config not found: {path}; create it from workspace.example.toml")
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise WorkspaceError(f"invalid workspace config: {path}: {exc}") from exc
        schema_version = raw.get("schema_version")
        if schema_version not in {SCHEMA_VERSION, PORTABLE_SCHEMA_VERSION}:
            raise WorkspaceError(f"unsupported workspace schema_version: {raw.get('schema_version')!r}")
        workspace_id = raw.get("workspace_id")
        if workspace_id is not None and validate_workspace_id(workspace_id) is None:
            raise WorkspaceError("workspace_id must be a non-empty persistent logical identifier, not a placeholder")
        paths, obs = raw.get("paths", {}), raw.get("obsidian", {})
        root = path.parent.resolve()
        if schema_version == PORTABLE_SCHEMA_VERSION:
            if validate_workspace_id(workspace_id) is None:
                raise WorkspaceError("workspace v2 requires a persistent workspace_id")
            roles = {name: _declared_path(root, paths, name) for name in
                     ("project", "results", "sources", "derived", "local")}
            if len(set(roles.values())) != len(roles):
                raise WorkspaceError("workspace v2 role roots must be distinct")
            tools = raw.get("tools", {})
            pyvideotrans = tools.get("pyvideotrans", {}) if isinstance(tools, dict) else {}
            pyvideotrans_python = _optional_tool_path(root, pyvideotrans, "python")
            pyvideotrans_cli = _optional_tool_path(root, pyvideotrans, "cli")
            # Legacy fields remain usable by media-only commands during explicit migration.
            return cls(path, source, root, roles["project"], roles["sources"], roles["derived"],
                       roles["derived"] / "generated", roles["derived"] / "threads",
                       roles["derived"] / "concepts", roles["derived"] / "REVIEW.md",
                       validate_workspace_id(workspace_id), pyvideotrans_python, pyvideotrans_cli, schema_version,
                       roles["results"], roles["sources"], roles["derived"], roles["local"])
        project = _contained(root / _required(paths, "project"), root, "project")
        media = _contained(root / _required(paths, "media"), root, "media")
        vault = _contained(root / _required(paths, "obsidian"), root, "obsidian")
        generated = _contained(vault / _required(obs, "generated"), vault, "generated")
        threads = _contained(vault / _required(obs, "threads"), vault, "threads")
        concepts = _contained(vault / _required(obs, "concepts"), vault, "concepts")
        review = _contained(vault / _required(obs, "review"), vault, "review")
        tools = raw.get("tools", {})
        pyvideotrans = tools.get("pyvideotrans", {}) if isinstance(tools, dict) else {}
        pyvideotrans_python = _optional_tool_path(root, pyvideotrans, "python")
        pyvideotrans_cli = _optional_tool_path(root, pyvideotrans, "cli")
        if generated == vault or any(generated == item or generated in item.parents for item in (threads, concepts, review, vault / "收件箱")):
            raise WorkspaceError("generated path overlaps a protected Vault boundary")
        return cls(path, source, root, project, media, vault, generated, threads, concepts, review,
                   validate_workspace_id(workspace_id),
                   pyvideotrans_python, pyvideotrans_cli, schema_version)

    def as_dict(self) -> dict[str, Any]:
        if self.schema_version == PORTABLE_SCHEMA_VERSION:
            return {"ok": True, "schema_version": self.schema_version, "workspace_id": self.workspace_id,
                    "config_source": self.source, "config": str(self.config_path),
                    "root": str(self.root), "roles": {name: str(getattr(self, name)) for name in
                    ("project", "results", "sources", "derived", "local")},
                    "tools": {"pyvideotrans": {
                        "python": str(self.pyvideotrans_python) if self.pyvideotrans_python else None,
                        "cli": str(self.pyvideotrans_cli) if self.pyvideotrans_cli else None,
                    }}}
        return {"ok": True, "schema_version": SCHEMA_VERSION, "workspace_id": self.workspace_id,
                "config_source": self.source,
                "config": str(self.config_path), "root": str(self.root), "project": str(self.project),
                "media": str(self.media), "obsidian": str(self.obsidian), "generated": str(self.generated),
                "threads": str(self.threads), "concepts": str(self.concepts), "review": str(self.review),
                "tools": {"pyvideotrans": {
                    "python": str(self.pyvideotrans_python) if self.pyvideotrans_python else None,
                    "cli": str(self.pyvideotrans_cli) if self.pyvideotrans_cli else None,
                }}}


def _declared_path(root: Path, mapping: dict[str, Any], key: str) -> Path:
    value = Path(_required(mapping, key)).expanduser()
    return (value if value.is_absolute() else root / value).resolve(strict=False)


def _required(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceError(f"missing required workspace setting: {key}")
    return value


def _optional_tool_path(root: Path, mapping: Any, key: str) -> Path | None:
    if not isinstance(mapping, dict):
        return None
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceError(f"invalid optional workspace setting: tools.pyvideotrans.{key}")
    path = Path(value).expanduser()
    candidate = path if path.is_absolute() else root / path
    return Path(os.path.abspath(candidate))


def discover_workspace(explicit: Path | None = None, cwd: Path | None = None, env: dict[str, str] | None = None,
                       locator: Path | None = None) -> WorkspaceConfig:
    env = os.environ if env is None else env
    if explicit is not None:
        return WorkspaceConfig.load(explicit, "explicit")
    if env.get("VIDEO_EXTRACT_WORKSPACE"):
        return WorkspaceConfig.load(Path(env["VIDEO_EXTRACT_WORKSPACE"]), "environment")
    current = (cwd or Path.cwd()).expanduser().resolve()
    for directory in (current, *current.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return WorkspaceConfig.load(candidate, "ancestor")
    locator = locator or LOCATOR
    if locator.is_file():
        raw = tomllib.loads(locator.read_text(encoding="utf-8"))
        value = raw.get("workspace")
        if isinstance(value, str) and Path(value).expanduser().is_absolute():
            return WorkspaceConfig.load(Path(value), "locator")
        raise WorkspaceError(f"locator must contain an absolute workspace path: {locator}")
    raise WorkspaceError("workspace.toml not found; pass --workspace, set VIDEO_EXTRACT_WORKSPACE, or create ~/.config/video-extract/config.toml")


def _broken_images(root: Path) -> list[str]:
    import re
    broken: list[str] = []
    if not root.is_dir(): return broken
    for note in root.rglob("*.md"):
        for target in re.findall(r"!\[[^]]*\]\(([^)]+)\)", note.read_text(encoding="utf-8", errors="replace")):
            if "://" not in target and not (note.parent / target).resolve().is_file(): broken.append(f"{note}:{target}")
    return broken


def _old_path_refs(config: WorkspaceConfig) -> dict[str, list[str]]:
    needles = ("/Users/syz/Media/video-extract", "/Users/syz/code/obsidian_本地知识库", "/Users/syz/code/video-extract-core")
    groups = {"live": [], "historical": []}
    roots = [config.project, Path.home() / ".agents/skills/extract-media", Path.home() / ".agents/skills/source-notes", Path.home() / ".agents/skills/mandarin-audio"]
    for root in roots:
        if not root.exists(): continue
        for path in root.rglob("*"):
            if not path.is_file() or path.stat().st_size > 2_000_000 or any(part in {".git", ".venv", "__pycache__"} for part in path.parts): continue
            try: text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError): continue
            if any(value in text for value in needles):
                bucket = "historical" if "migration" in path.name.lower() or "execution-plan" in path.name else "live"
                groups[bucket].append(str(path))
    return groups


def doctor(config: WorkspaceConfig) -> dict[str, Any]:
    from .library import audit_library_packages, library_status
    from .playlists import verify_playback
    packages = audit_library_packages(config.media) if (config.media / "items").is_dir() else {"ok": False, "counts": {"migration_regression": 0}}
    library = library_status(config.media)
    playback = verify_playback(config.media / "playback")
    broken_images = _broken_images(config.generated)
    marker = config.generated / MANAGED_MARKER
    marker_ok = marker.is_file() if config.generated.exists() else True
    protected = {name: str(path) for name, path in (("threads", config.threads), ("concepts", config.concepts), ("review", config.review), ("inbox", config.obsidian / "收件箱"))}
    refs = _old_path_refs(config)
    podcast = {
        "configured": bool(config.pyvideotrans_python and config.pyvideotrans_cli),
        "python_exists": bool(config.pyvideotrans_python and config.pyvideotrans_python.is_file()),
        "cli_exists": bool(config.pyvideotrans_cli and config.pyvideotrans_cli.is_file()),
    }
    podcast["available"] = podcast["configured"] and podcast["python_exists"] and podcast["cli_exists"]
    result = {"ok": packages.get("counts", {}).get("migration_regression", 0) == 0 and library.get("missing_paths", 0) == 0 and playback.get("ok", False) and not broken_images and marker_ok,
              "workspace": config.as_dict(), "packages": packages.get("counts", {}), "library": library,
              "playback": playback, "obsidian": {"managed_marker": marker_ok, "broken_images": len(broken_images), "broken_image_details": broken_images, "protected": protected},
              "podcast_capability": podcast, "old_path_references": refs, "migration_regression": packages.get("counts", {}).get("migration_regression", 0)}
    return result


def _manifest(root: Path) -> list[dict[str, Any]]:
    entries = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() or p.is_symlink()):
        rel = str(path.relative_to(root)); stat = path.lstat()
        entries.append({"path": rel, "size": stat.st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None})
    return entries


def normalize_export_manifests(media: Path, generated: Path) -> dict[str, Any]:
    """Point package export metadata at the activated generated root."""
    from .manifest import atomic_write_json, read_json
    notes: dict[str, list[Path]] = {}
    for note in generated.rglob("*.md"):
        for parent in note.parents:
            if "__" in parent.name:
                notes.setdefault(parent.name.rsplit("__", 1)[-1], []).append(note)
                break
    updated = ambiguous = 0
    for export_path in sorted((media / "items").glob("*/*/export-manifest.json")):
        package = export_path.parent; manifest = read_json(package / "manifest.json")
        item_id = str(manifest.get("identity") or package.name); matches = notes.get(item_id, [])
        data = read_json(export_path)
        if len(matches) != 1:
            recorded = Path(str(data.get("note", "")))
            parts = recorded.parts
            marker_index = next((index for index, part in enumerate(parts) if part.startswith(".video-extract-generated-")), None)
            if marker_index is not None:
                candidate = generated.joinpath(*parts[marker_index + 1:])
                if candidate.is_file(): matches = [candidate]
        if len(matches) != 1:
            ambiguous += 1; continue
        data["package"] = str(package); data["note"] = str(matches[0])
        atomic_write_json(export_path, data); updated += 1
    return {"ok": ambiguous == 0, "updated": updated, "ambiguous": ambiguous}


def rebuild(config: WorkspaceConfig, apply: bool) -> dict[str, Any]:
    from .library import audit_library_packages, rebuild_library
    from .obsidian_export import export_all_verified
    from .playlists import build_playback_views, verify_playback
    preflight = audit_library_packages(config.media)
    card_schedules: list[dict[str, Any]] = []
    if config.schema_version == PORTABLE_SCHEMA_VERSION:
        from .cards import _load as load_cards, schedule as card_schedule
        from .review import _load as load_review
        card_snapshot = load_cards(config)
        review_events = load_review(config)["record"]["events"].values()
        for card in card_snapshot["record"]["cards"].values():
            for version_id in card["versions"]:
                event_dates = [event.get("review_date") or event["created_at"][:10]
                               for event in review_events
                               if event["pin"].get("card_version_id") == version_id]
                as_of = max(event_dates) if event_dates else card["versions"][version_id]["effective_date"]
                card_schedules.append(card_schedule(config, card_version_id=version_id,
                                                    on_date=as_of)["result"])
    plan = {"sqlite": str(config.media / "catalog/library.sqlite"), "obsidian": str(config.generated),
            "playback": str(config.media / "playback"), "card_schedules": "derive from card/review facts"}
    if not apply:
        return {"ok": preflight.get("counts", {}).get("migration_regression", 0) == 0, "dry_run": True,
                "written": False, "plan": plan, "packages": preflight.get("counts", {}),
                "card_schedules": card_schedules}
    config.generated.parent.mkdir(parents=True, exist_ok=True)
    protected_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for base in (config.threads, config.concepts) if base.exists() for p in base.rglob("*") if p.is_file()}
    if config.review.is_file(): protected_hashes[str(config.review)] = hashlib.sha256(config.review.read_bytes()).hexdigest()
    library = rebuild_library(config.media)
    staged = Path(tempfile.mkdtemp(prefix=".video-extract-generated-", dir=config.generated.parent))
    try:
        exported = export_all_verified(config.media, staged)
        manifest = _manifest(staged)
        (staged / MANAGED_MARKER).write_text(json.dumps({"workspace_schema": SCHEMA_VERSION, "generator": "video-extract", "rebuilt_at": datetime.now(timezone.utc).isoformat(), "entries": len(manifest), "manifest": manifest}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if config.generated.exists():
            marker = config.generated / MANAGED_MARKER
            if not marker.is_file(): raise WorkspaceError(f"refusing to replace unmarked generated directory: {config.generated}")
            backup = config.generated.with_name(config.generated.name + ".previous")
            if backup.exists(): shutil.rmtree(backup)
            os.replace(config.generated, backup); os.replace(staged, config.generated); shutil.rmtree(backup)
        else: os.replace(staged, config.generated)
    finally:
        if staged.exists(): shutil.rmtree(staged)
    after_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for base in (config.threads, config.concepts) if base.exists() for p in base.rglob("*") if p.is_file()}
    if config.review.is_file(): after_hashes[str(config.review)] = hashlib.sha256(config.review.read_bytes()).hexdigest()
    if protected_hashes != after_hashes: raise WorkspaceError("rebuild changed protected learning artifacts")
    normalized_exports = normalize_export_manifests(config.media, config.generated)
    if not normalized_exports["ok"]: raise WorkspaceError(f"ambiguous activated exports: {normalized_exports}")
    library = rebuild_library(config.media)
    playback_live = config.media / "playback"
    playback_quarantine = config.media / "migration-quarantine/workspace-refactor-2026-09-16/playback-pre-rebuild"
    if playback_quarantine.exists(): raise WorkspaceError(f"playback quarantine target already exists: {playback_quarantine}")
    real_media = [str(path) for path in playback_live.rglob("*") if path.is_file() and not path.is_symlink() and path.suffix.lower() in {".mp4", ".mov", ".mkv", ".wav", ".m4a", ".mp3"}]
    if real_media: raise WorkspaceError(f"refusing playback transaction containing real media: {real_media}")
    staged_playback = Path(tempfile.mkdtemp(prefix=".playback-rebuild-", dir=config.media))
    playlists = []
    for catalog in sorted((config.media / "collections").glob("*/*/catalog.json")):
        playlists.append(build_playback_views(catalog, config.media, staged_playback))
    playback_check = verify_playback(staged_playback)
    if not all(item.get("ok") for item in playlists) or not playback_check.get("ok"):
        raise WorkspaceError(f"staged playback validation failed: {playback_check}")
    journal = config.root / "migration-journal.jsonl"
    def journal_event(event: dict[str, Any]) -> None:
        with journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), **event}, ensure_ascii=False) + "\n")
            stream.flush(); os.fsync(stream.fileno())
    playback_quarantine.parent.mkdir(parents=True, exist_ok=True)
    before = playback_live.stat(); journal_event({"event": "planned", "kind": "playback-quarantine", "source": str(playback_live), "target": str(playback_quarantine), "dev": before.st_dev, "inode": before.st_ino, "mtime_ns": before.st_mtime_ns})
    os.rename(playback_live, playback_quarantine)
    after = playback_quarantine.stat(); journal_event({"event": "completed", "kind": "playback-quarantine", "source": str(playback_live), "target": str(playback_quarantine), "dev": after.st_dev, "inode": after.st_ino, "mtime_ns": after.st_mtime_ns})
    staged_before = staged_playback.stat(); journal_event({"event": "planned", "kind": "playback-activate", "source": str(staged_playback), "target": str(playback_live), "dev": staged_before.st_dev, "inode": staged_before.st_ino, "mtime_ns": staged_before.st_mtime_ns})
    os.rename(staged_playback, playback_live)
    staged_after = playback_live.stat(); journal_event({"event": "completed", "kind": "playback-activate", "source": str(staged_playback), "target": str(playback_live), "dev": staged_after.st_dev, "inode": staged_after.st_ino, "mtime_ns": staged_after.st_mtime_ns})
    final = doctor(config)
    return {"ok": bool(exported.get("ok")) and all(item.get("ok") for item in playlists) and final["ok"], "dry_run": False, "written": True, "library": library, "export": exported, "normalized_exports": normalized_exports, "playlists": playlists, "card_schedules": card_schedules, "doctor": final}


def prepare_migration(config: WorkspaceConfig, full_hash: bool = False) -> dict[str, Any]:
    must = [config.media / "items", config.media / "collections", config.obsidian, config.config_path]
    rebuildable = [config.media / "catalog/library.sqlite", config.media / "playback", config.generated]
    records = []
    for root in must:
        if root.is_file(): paths = [root]
        elif root.is_dir(): paths = [p for p in root.rglob("*") if p.is_file() and not (root == config.obsidian and config.generated in p.parents)]
        else: paths = []
        for path in paths:
            stat = path.stat(); record = {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            if full_hash or path.suffix.lower() in {".md", ".json", ".toml"}: record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            records.append(record)
    return {"ok": all(path.exists() for path in must), "must_migrate": [str(x) for x in must], "rebuildable": [str(x) for x in rebuildable], "files": records,
            "restore": ["copy Media and Vault to the new machine", "clone video-extract-core", "write ~/.config/video-extract/config.toml", "run video-extract workspace rebuild --apply --json", "run video-extract workspace doctor --json"]}
