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
CONFIG_NAME = "workspace.toml"
LOCATOR = Path("~/.config/video-extract/config.toml").expanduser()
MANAGED_MARKER = ".video-extract-managed.json"
PROTECTED_VAULT_NAMES = {"threads", "concepts", "REVIEW.md", "收件箱"}


class WorkspaceError(RuntimeError):
    pass


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

    @classmethod
    def load(cls, path: Path, source: str = "explicit") -> "WorkspaceConfig":
        path = path.expanduser().resolve()
        if not path.is_file():
            raise WorkspaceError(f"workspace config not found: {path}; create it from workspace.example.toml")
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise WorkspaceError(f"unsupported workspace schema_version: {raw.get('schema_version')!r}")
        paths, obs = raw.get("paths", {}), raw.get("obsidian", {})
        root = path.parent.resolve()
        project = _contained(root / _required(paths, "project"), root, "project")
        media = _contained(root / _required(paths, "media"), root, "media")
        vault = _contained(root / _required(paths, "obsidian"), root, "obsidian")
        generated = _contained(vault / _required(obs, "generated"), vault, "generated")
        threads = _contained(vault / _required(obs, "threads"), vault, "threads")
        concepts = _contained(vault / _required(obs, "concepts"), vault, "concepts")
        review = _contained(vault / _required(obs, "review"), vault, "review")
        if generated == vault or any(generated == item or generated in item.parents for item in (threads, concepts, review, vault / "收件箱")):
            raise WorkspaceError("generated path overlaps a protected Vault boundary")
        return cls(path, source, root, project, media, vault, generated, threads, concepts, review)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": True, "schema_version": SCHEMA_VERSION, "config_source": self.source,
                "config": str(self.config_path), "root": str(self.root), "project": str(self.project),
                "media": str(self.media), "obsidian": str(self.obsidian), "generated": str(self.generated),
                "threads": str(self.threads), "concepts": str(self.concepts), "review": str(self.review)}


def _required(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceError(f"missing required workspace setting: {key}")
    return value


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
    roots = [config.project, Path.home() / ".agents/skills/video-learning"]
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
    result = {"ok": packages.get("counts", {}).get("migration_regression", 0) == 0 and library.get("missing_paths", 0) == 0 and playback.get("ok", False) and not broken_images and marker_ok,
              "workspace": config.as_dict(), "packages": packages.get("counts", {}), "library": library,
              "playback": playback, "obsidian": {"managed_marker": marker_ok, "broken_images": len(broken_images), "broken_image_details": broken_images, "protected": protected},
              "old_path_references": refs, "migration_regression": packages.get("counts", {}).get("migration_regression", 0)}
    return result


def _manifest(root: Path) -> list[dict[str, Any]]:
    entries = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() or p.is_symlink()):
        rel = str(path.relative_to(root)); stat = path.lstat()
        entries.append({"path": rel, "size": stat.st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None})
    return entries


def rebuild(config: WorkspaceConfig, apply: bool) -> dict[str, Any]:
    from .library import audit_library_packages, rebuild_library
    from .obsidian_export import export_all_verified
    preflight = audit_library_packages(config.media)
    plan = {"sqlite": str(config.media / "catalog/library.sqlite"), "obsidian": str(config.generated), "playback": str(config.media / "playback")}
    if not apply:
        return {"ok": preflight.get("counts", {}).get("migration_regression", 0) == 0, "dry_run": True, "written": False, "plan": plan, "packages": preflight.get("counts", {})}
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
    final = doctor(config)
    return {"ok": bool(exported.get("ok")) and final["ok"], "dry_run": False, "written": True, "library": library, "export": exported, "doctor": final}


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
