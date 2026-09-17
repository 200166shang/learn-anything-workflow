"""Deterministic, journaled migration of legacy media packages by same-device rename."""

from __future__ import annotations

import errno
import copy
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

from .contracts import DEFAULT_ARTIFACTS
from .manifest import atomic_write_json, package_path, read_json, sanitize
from .validate import validate, validate_media_package_state, validate_media_request

CLASSIFICATIONS = {"move", "reuse_duplicate", "derived_ignore", "quarantine", "conflict"}
MEDIA_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".mp3", ".m4a", ".wav", ".srt", ".vtt"}
PACKAGE_DIRS = {"source", "media", "subtitles", "frames", "review", "notes", "evidence", "translation", "audio", "listening", "localization"}
DERIVED_PARTS = {".venv", "__pycache__", ".pytest_cache", ".git", "node_modules", ".mypy_cache", ".ruff_cache"}
CATALOG_NAMES = {"course_catalog.json", "catalog.json", "collection.json"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stat(path: Path) -> dict[str, int]:
    value = path.lstat()
    return {"dev": value.st_dev, "ino": value.st_ino, "size": value.st_size, "mtime_ns": value.st_mtime_ns}


def _nearest_existing(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "unknown"


def _identity_from_url(value: str) -> tuple[str | None, str | None]:
    match = re.search(r"/(v_[A-Za-z0-9]+)(?:[/?#]|$)", value)
    if match:
        return "xiaoe", match.group(1)
    match = re.search(r"/(BV[A-Za-z0-9]+)(?:[/?#]|$)", value)
    if match:
        page = parse_qs(urlparse(value).query).get("p", [None])[0]
        return "bilibili", f"{match.group(1)}_p{page or 1}"
    match = re.search(r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|shorts/|embed/))([A-Za-z0-9_-]{11})", value)
    if match:
        return "youtube", match.group(1)
    return None, None


def _valid_identity(platform: str | None, identity: Any) -> bool:
    if not isinstance(identity, str) or not identity or "/" in identity or "\\" in identity:
        return False
    if platform == "xiaoe":
        return bool(re.fullmatch(r"v_[A-Za-z0-9]+", identity))
    if platform == "bilibili":
        return bool(re.fullmatch(r"BV[A-Za-z0-9]+(?:_p\d+)?", identity))
    if platform == "youtube":
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", identity))
    if platform == "local":
        return bool(re.fullmatch(r"[a-f0-9]{64}", identity))
    return False


@dataclass
class CatalogEntry:
    platform: str
    identity: str
    title: str
    collection_id: str
    collection_title: str
    section: str | None = None
    ordinal: int | None = None


@dataclass
class PackageDescriptor:
    root: str
    platform: str
    item_id: str
    title: str
    collection_id: str | None = None
    collection_title: str | None = None
    section: str | None = None
    ordinal: int | None = None
    schema_version: int | None = None


def _catalog_identity(data: dict[str, Any], path: Path, platform: str) -> str:
    values = [data.get("identity"), data.get("id"), data.get("bvid"), data.get("course_url"), data.get("source_url")]
    for value in values:
        if not isinstance(value, str):
            continue
        if platform == "xiaoe":
            match = re.search(r"course_[A-Za-z0-9]+", value)
            if match:
                return match.group(0)
        if platform == "bilibili":
            match = re.search(r"BV[A-Za-z0-9]+", value)
            if match:
                return match.group(0)
        if platform == "youtube" and re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
            return value
    return _safe_id(path.parent.name)


def _infer_platform(path: Path, source_root: Path, data: dict[str, Any] | None = None) -> str:
    if data and data.get("platform") in {"xiaoe", "bilibili", "youtube", "local"}:
        return str(data["platform"])
    try:
        parts = path.relative_to(source_root).parts
    except ValueError:
        parts = path.parts
    for platform in ("xiaoe", "bilibili", "youtube", "local"):
        if platform in parts:
            return platform
    text = str(path).lower()
    if "xiaoe" in text or "小鹅" in text:
        return "xiaoe"
    if "bilibili" in text:
        return "bilibili"
    if "youtube" in text:
        return "youtube"
    return "local"


def _load_catalogs(source_root: Path) -> tuple[list[dict[str, Any]], dict[str, list[CatalogEntry]]]:
    catalogs: list[dict[str, Any]] = []
    by_title: dict[str, list[CatalogEntry]] = {}
    for path in sorted(p for p in source_root.rglob("*.json") if p.name in CATALOG_NAMES and p.is_file()):
        try:
            data = read_json(path)
        except Exception:
            continue
        platform = _infer_platform(path, source_root, data)
        items = data.get("lessons") if isinstance(data.get("lessons"), list) else data.get("items")
        if not isinstance(items, list):
            continue
        collection_id = _catalog_identity(data, path, platform)
        collection_title = str(data.get("course_title") or data.get("title") or path.parent.name)
        catalogs.append({"path": str(path), "platform": platform, "collection_id": collection_id, "collection_title": collection_title})
        for position, item in enumerate(items, 1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            identity = str(item.get("video_id") or item.get("id") or item.get("bvid") or "")
            if platform == "bilibili" and identity.startswith("BV") and not re.search(r"_p\d+$", identity):
                identity += f"_p{item.get('page') or 1}"
            if not _valid_identity(platform, identity):
                for value in (item.get("url"), item.get("source_url")):
                    found_platform, found_id = _identity_from_url(str(value or ""))
                    if found_platform == platform and found_id:
                        identity = found_id
                        break
            if title and _valid_identity(platform, identity):
                entry = CatalogEntry(platform, identity, title, collection_id, collection_title, item.get("section"), int(item.get("sort_value") or item.get("index") or position))
                by_title.setdefault(title, []).append(entry)
    return catalogs, by_title


def _metadata_candidates(package: Path) -> Iterable[dict[str, Any]]:
    for relative in ("metadata.json", "source/metadata.json", "media/metadata.json"):
        path = package / relative
        if path.is_file():
            try:
                yield read_json(path)
            except Exception:
                continue


def _manifest(package: Path) -> dict[str, Any]:
    path = package / "manifest.json"
    if not path.is_file():
        return {}
    try:
        return read_json(path)
    except Exception:
        return {}


def _discover_packages(source_root: Path, catalog_by_title: dict[str, list[CatalogEntry]]) -> list[Path]:
    roots: set[Path] = set()
    manifests = sorted(source_root.rglob("manifest.json"), key=lambda p: len(p.parts))
    for path in manifests:
        package = path.parent
        if not any(parent in roots for parent in package.parents):
            roots.add(package)
    for media in sorted(p for p in source_root.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES):
        if any(root == media or root in media.parents for root in roots):
            continue
        package = media.parent.parent if media.parent.name in PACKAGE_DIRS else media.parent
        if any(parent in roots for parent in package.parents):
            continue
        roots.add(package)
    return sorted(roots, key=lambda p: str(p))


def _descriptor(package: Path, source_root: Path, catalog_by_title: dict[str, list[CatalogEntry]]) -> PackageDescriptor | None:
    manifest = _manifest(package)
    title = str(manifest.get("title") or package.name)
    catalog_matches = catalog_by_title.get(title, [])
    if len(catalog_matches) == 1:
        match = catalog_matches[0]
        return PackageDescriptor(str(package), match.platform, match.identity, title, match.collection_id, match.collection_title, match.section, match.ordinal, manifest.get("schema_version"))
    for metadata in _metadata_candidates(package):
        title = str(manifest.get("title") or metadata.get("title") or package.name)
        for value in (metadata.get("source_url"), metadata.get("url"), metadata.get("source")):
            platform, identity = _identity_from_url(str(value or ""))
            if platform and identity:
                matches = catalog_by_title.get(title, [])
                match = next((x for x in matches if x.identity == identity), None)
                return PackageDescriptor(str(package), platform, identity, title, match.collection_id if match else None, match.collection_title if match else None, match.section if match else None, match.ordinal if match else None, manifest.get("schema_version"))
    platform = _infer_platform(package, source_root, manifest)
    identity = manifest.get("identity") or manifest.get("video_id")
    if platform == "bilibili" and isinstance(identity, str) and identity.startswith("BV") and not re.search(r"_p\d+$", identity):
        identity += "_p1"
    if _valid_identity(platform, identity):
        return PackageDescriptor(str(package), platform, str(identity), title, schema_version=manifest.get("schema_version"))
    if platform == "local":
        videos = sorted(p for p in package.rglob("*") if p.is_file() and p.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"})
        if videos:
            return PackageDescriptor(str(package), "local", _sha256(videos[0]), title, schema_version=manifest.get("schema_version"))
    return None


def _escapes_manifest(package: Path, manifest: dict[str, Any]) -> bool:
    for value in (manifest.get("artifacts") or {}).values():
        if not isinstance(value, str):
            continue
        try:
            package_path(package, value)
        except ValueError:
            return True
    return False


def _destination_relative(package: Path, path: Path) -> Path:
    relative = path.relative_to(package)
    if len(relative.parts) == 1:
        suffix = path.suffix.lower()
        if path.name == "manifest.json":
            return Path("manifest.json")
        if path.name == "metadata.json":
            return Path("source/metadata.json")
        if path.name == "notes.md":
            return Path("notes/notes.md")
        if suffix in {".mp4", ".mov", ".mkv", ".webm"}:
            return Path("source/source" + suffix)
        if suffix in {".wav", ".m4a", ".mp3"}:
            return Path("source/source" + suffix)
        if suffix in {".srt", ".vtt"}:
            return Path("source/transcript" + suffix)
    return relative


def _all_files(source_root: Path) -> list[Path]:
    files: list[Path] = []
    for directory, names, filenames in os.walk(source_root, followlinks=False):
        base = Path(directory)
        names[:] = [name for name in names if not (base / name).is_symlink()]
        files.extend(base / name for name in filenames)
        files.extend(base / name for name in names if (base / name).is_symlink())
    return sorted(files, key=lambda p: str(p))


def _base_operation(path: Path, source_root: Path) -> dict[str, Any]:
    return {"source": str(path), "relative_source": path.relative_to(source_root).as_posix(), "source_stat": _stat(path), "classification": "derived_ignore", "reason": "not a long-term media asset"}


def _resolve_audited_collision(group: list[dict[str, Any]], destination: Path, quarantine_root: Path) -> bool:
    """Resolve only collision shapes covered by the checked conflict audit."""
    if len(group) != 2:
        return False
    sources = [Path(op["source"]) for op in group]
    hashes = [_sha256(path) for path in sources]
    if destination.name == ".DS_Store":
        for op, digest in zip(group, hashes):
            op.update(classification="quarantine", destination=str(quarantine_root / "derived" / digest[:12] / ".DS_Store"), reason="audited derived metadata collision preserved")
        return True
    text = destination.as_posix()
    canonical_index: int | None = None
    if "BV11X4y1j7si_p41" in text:
        if destination.name == "manifest.json":
            canonical_index = next((i for i, path in enumerate(sources) if "/bilibili/9.2 " in path.as_posix()), None)
        elif destination.name in {"article.md", "metadata.json", "transcript.srt", "transcript.txt"}:
            canonical_index = next((i for i, path in enumerate(sources) if "__BV11X4y1j7si/041_" in path.as_posix()), None)
    elif "BV11X4y1j7si_p42" in text:
        canonical_index = next((i for i, path in enumerate(sources) if "/bilibili/9.3 " in path.as_posix()), None)
    elif "BV11X4y1j7si_p43" in text:
        canonical_index = next((i for i, path in enumerate(sources) if "/bilibili/9.4 " in path.as_posix()), None)
    elif destination.as_posix().endswith("/notes/notes.md"):
        canonical_index = next((i for i, path in enumerate(sources) if path.parent.name == "notes"), None)
    elif destination.as_posix().endswith("/source/metadata.json"):
        canonical_index = next((i for i, path in enumerate(sources) if path.parent.name == "source"), None)
    if canonical_index is None:
        return False
    variant_index = 1 - canonical_index
    variant = group[variant_index]; digest = hashes[variant_index]
    variant.update(classification="move", destination=str(destination.parent / "legacy-variants" / digest[:12] / destination.name), reason="audited non-canonical variant preserved")
    group[canonical_index].update(classification="move", reason="audited canonical source")
    return True


def _resolve_audited_existing_target(op: dict[str, Any], destination: Path) -> bool:
    source = Path(op["source"])
    if destination.as_posix().endswith("/items/youtube/jwChiek_aRY/manifest.json") and source.as_posix().endswith("/output/jwChiek_aRY/manifest.json"):
        digest = _sha256(source)
        op.update(classification="move", destination=str(destination.parent / "legacy-variants" / digest[:12] / "manifest.json"), reason="audited older-layout manifest preserved; existing provenance-rich canonical retained")
        return True
    return False


def build_plan(source_root: Path, library_root: Path, *, quarantine_root: Path | None = None, sessions_root: Path | None = None, root_media_only: bool = False) -> dict[str, Any]:
    source_root = source_root.expanduser().resolve()
    library_root = library_root.expanduser().resolve()
    quarantine_root = (quarantine_root or library_root / "migration/quarantine").expanduser().resolve()
    sessions_root = (sessions_root or Path("/Users/syz/Library/Application Support/video-extract/sessions")).expanduser().resolve()
    if not source_root.is_dir():
        raise ValueError(f"source root is not readable: {source_root}")
    source_device = source_root.stat().st_dev
    target_device = _nearest_existing(library_root).stat().st_dev
    if source_device != target_device:
        raise RuntimeError("source and library are on different devices")

    catalogs, catalog_by_title = ([], {}) if root_media_only else _load_catalogs(source_root)
    packages = [] if root_media_only else _discover_packages(source_root, catalog_by_title)
    descriptors = {package: _descriptor(package, source_root, catalog_by_title) for package in packages}
    package_files: dict[Path, list[Path]] = {package: [] for package in packages}
    operations: list[dict[str, Any]] = []
    all_files = sorted((p for p in source_root.iterdir() if p.is_file() and (p.suffix.lower() in MEDIA_SUFFIXES or p.name.endswith(".danmaku.xml"))), key=lambda p: str(p)) if root_media_only else _all_files(source_root)
    catalog_paths = {Path(x["path"]): x for x in catalogs}

    for path in all_files:
        op = _base_operation(path, source_root)
        if root_media_only:
            youtube = re.search(r"\[([A-Za-z0-9_-]{11})\]", path.name)
            bilibili = re.search(r"\[(BV[A-Za-z0-9]+_p\d+)\]", path.name)
            if youtube and path.suffix.lower() in {".vtt", ".srt"}:
                destination = library_root / "items/youtube" / youtube.group(1) / "source/legacy-subtitles" / path.name
                op.update(classification="move", destination=str(destination), reason="identified root subtitle", platform="youtube", item_id=youtube.group(1))
            elif bilibili and path.name.endswith(".danmaku.xml"):
                destination = library_root / "items/bilibili" / bilibili.group(1) / "source/danmaku.xml"
                op.update(classification="move", destination=str(destination), reason="identified root danmaku", platform="bilibili", item_id=bilibili.group(1))
            else:
                op.update(classification="conflict", reason="root media identity cannot be determined")
            operations.append(op); continue
        if path.is_symlink():
            op.update(classification="derived_ignore", reason="derived symlink")
            operations.append(op); continue
        relative_parts = set(path.relative_to(source_root).parts)
        session_name = next((name for name in ("browser_session", "bilibili_browser_session") if name in path.parts), None)
        if session_name:
            marker = path.parts.index(session_name)
            destination = sessions_root / session_name / Path(*path.parts[marker + 1:])
            op.update(classification="move", destination=str(destination), reason="browser session relocation")
            operations.append(op); continue
        if relative_parts & DERIVED_PARTS or "playback" in relative_parts:
            op.update(classification="derived_ignore", reason="rebuildable cache or derived playback")
            operations.append(op); continue
        if path.name == "tencent-tts-preview-feijing.mp3" and path.parent == source_root:
            digest = _sha256(path)
            op.update(classification="quarantine", destination=str(quarantine_root / "derived-preserved" / digest[:12] / path.name), reason="audited short TTS preview artifact preserved")
            operations.append(op); continue
        if path in catalog_paths:
            catalog = catalog_paths[path]
            destination = library_root / "collections" / catalog["platform"] / catalog["collection_id"] / "catalog.json"
            op.update(classification="move", destination=str(destination), reason="collection catalog", **{k: catalog[k] for k in ("platform", "collection_id", "collection_title")})
            operations.append(op); continue
        package = next((candidate for candidate in sorted(packages, key=lambda p: len(p.parts), reverse=True) if candidate == path.parent or candidate in path.parents), None)
        if package:
            package_files[package].append(path)
            descriptor = descriptors[package]
            if descriptor is None:
                op.update(classification="conflict", reason="package identity cannot be determined")
            elif path.name == "manifest.json" and _escapes_manifest(package, _manifest(package)):
                op.update(classification="conflict", reason="manifest artifact path escapes package", item_id=descriptor.item_id, platform=descriptor.platform)
            else:
                destination = library_root / "items" / descriptor.platform / descriptor.item_id / _destination_relative(package, path)
                op.update(classification="move", destination=str(destination), reason="canonical package asset", item_id=descriptor.item_id, platform=descriptor.platform, package_root=str(package))
            operations.append(op); continue
        if path.suffix.lower() in MEDIA_SUFFIXES:
            op.update(classification="quarantine", destination=str(quarantine_root / source_root.name / path.relative_to(source_root)), reason="media file without determinable package identity")
        elif source_root.name in {"work", "output", "outputs"}:
            op.update(classification="quarantine", destination=str(quarantine_root / source_root.name / path.relative_to(source_root)), reason="unknown legacy work file retained")
        else:
            op.update(classification="derived_ignore", reason="project or reading-view file outside a package")
        operations.append(op)

    # Resolve collisions against both existing targets and other planned sources.
    by_destination: dict[str, list[dict[str, Any]]] = {}
    for op in operations:
        if op["classification"] in {"move", "quarantine"} and op.get("destination"):
            by_destination.setdefault(op["destination"], []).append(op)
    for destination_text, group in by_destination.items():
        destination = Path(destination_text)
        hashes = [_sha256(Path(op["source"])) for op in group]
        if destination.exists():
            target_hash = _sha256(destination)
            if all(value == target_hash for value in hashes):
                for op, value in zip(group, hashes):
                    op.update(classification="reuse_duplicate", source_hash=value, target_hash=target_hash, reason="identical target already exists")
            elif len(group) == 1 and _resolve_audited_existing_target(group[0], destination):
                pass
            else:
                for op in group:
                    op.update(classification="conflict", reason="target exists with different content")
        elif len(group) > 1:
            if len(set(hashes)) == 1:
                group[0]["source_hash"] = hashes[0]
                for op in group[1:]:
                    op.update(classification="reuse_duplicate", source_hash=hashes[0], target_hash=hashes[0], depends_on_planned=True, reason="identical planned target")
            elif _resolve_audited_collision(group, destination, quarantine_root):
                pass
            else:
                for op in group:
                    op.update(classification="conflict", reason="multiple different sources map to one target")

    counts = {name: sum(op["classification"] == name for op in operations) for name in CLASSIFICATIONS}
    counts["unclassified"] = sum(op.get("classification") not in CLASSIFICATIONS for op in operations)
    counts["source_files"] = len(all_files)
    counts["accounted_percent"] = 100.0 if len(operations) == len(all_files) else (100 * len(operations) / len(all_files) if all_files else 100.0)
    return sanitize({
        "schema_version": 1,
        "created_ns": time.time_ns(),
        "source_root": str(source_root),
        "library_root": str(library_root),
        "quarantine_root": str(quarantine_root),
        "sessions_root": str(sessions_root),
        "devices": {"source": source_device, "library": target_device, "same_device": source_device == target_device},
        "catalogs": catalogs,
        "packages": [asdict(value) for value in descriptors.values() if value],
        "operations": operations,
        "summary": counts,
    })


def _stat_matches(path: Path, expected: dict[str, Any]) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    actual = _stat(path)
    return all(actual[key] == expected[key] for key in ("dev", "ino", "size", "mtime_ns"))


def _append_journal(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(sanitize(value), ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush(); os.fsync(stream.fileno())


def _normalize_manifest(package: Path, descriptor: dict[str, Any]) -> None:
    manifest_path = package / "manifest.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else {}
    if manifest.get("schema_version") == 5 and manifest.get("request", {}).get("type") == "media":
        return
    manifest["schema_version"] = 4
    manifest["platform"] = descriptor["platform"]
    manifest["identity"] = descriptor["item_id"]
    manifest["title"] = descriptor["title"]
    for key in ("collection_id", "collection_title", "section", "ordinal"):
        if descriptor.get(key) is not None:
            manifest[key] = descriptor[key]
    artifacts = manifest.setdefault("artifacts", {})
    for key, relative in DEFAULT_ARTIFACTS.items():
        if (package / relative).is_file():
            artifacts[key] = relative
    atomic_write_json(manifest_path, manifest)


def execute_plan(report: dict[str, Any], journal_path: Path) -> dict[str, Any]:
    if report.get("summary", {}).get("conflict") or report.get("summary", {}).get("unclassified"):
        raise RuntimeError("migration report contains conflicts or unclassified files")
    operations = report.get("operations") or []
    for op in operations:
        if not _stat_matches(Path(op["source"]), op["source_stat"]):
            raise RuntimeError(f"source changed since planning: {op['relative_source']}")
    source_device = int(report["devices"]["source"])
    target_device = _nearest_existing(Path(report["library_root"])).stat().st_dev
    if source_device != target_device:
        raise RuntimeError("source and library are now on different devices")

    actionable = [op for op in operations if op["classification"] in {"move", "quarantine"}]
    duplicates = [op for op in operations if op["classification"] == "reuse_duplicate"]
    moved = reused = inode_mismatches = 0
    for op in actionable:
        source, destination = Path(op["source"]), Path(op["destination"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        planned = {"event": "planned", "source": str(source), "destination": str(destination), "classification": op["classification"], "source_stat": op["source_stat"], "reverse": {"source": str(destination), "destination": str(source)}}
        _append_journal(journal_path, planned)
        try:
            os.rename(source, destination)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise RuntimeError("cross-device rename refused; copying is disabled") from exc
            raise
        after = _stat(destination)
        expected = op["source_stat"]
        if any(after[key] != expected[key] for key in ("dev", "ino", "size", "mtime_ns")):
            inode_mismatches += 1
            raise RuntimeError(f"rename stat mismatch: {op['relative_source']}")
        _append_journal(journal_path, {**planned, "event": "moved", "target_stat": after})
        moved += 1
    for op in duplicates:
        source, destination = Path(op["source"]), Path(op["destination"])
        _append_journal(journal_path, {"event": "planned", "source": str(source), "destination": str(destination), "classification": "reuse_duplicate", "source_stat": op["source_stat"], "reverse": None})
        if not destination.is_file() or _sha256(source) != _sha256(destination):
            raise RuntimeError(f"duplicate target changed: {op['relative_source']}")
        source.unlink()
        _append_journal(journal_path, {"event": "reused", "source": str(source), "destination": str(destination), "source_hash": op.get("source_hash")})
        reused += 1

    for descriptor in report.get("packages") or []:
        package = Path(report["library_root"]) / "items" / descriptor["platform"] / descriptor["item_id"]
        if package.exists():
            _normalize_manifest(package, descriptor)
    for source_root_text in report.get("source_roots") or [report["source_root"]]:
        source_root = Path(source_root_text)
        for directory in sorted((p for p in source_root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try: directory.rmdir()
            except OSError: pass
    validations: list[dict[str, Any]] = []
    for descriptor in report.get("packages") or []:
        package = Path(report["library_root"]) / "items" / descriptor["platform"] / descriptor["item_id"]
        if not (package / "manifest.json").exists():
            continue
        manifest = read_json(package / "manifest.json")
        try:
            result = validate_media_package_state(package) if manifest.get("schema_version") == 5 and manifest.get("request", {}).get("type") == "media" else validate(package).to_dict()
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        validations.append({"package": str(package), "validation": result})
    return {"ok": inode_mismatches == 0, "moved": moved, "reused": reused, "inode_mismatches": inode_mismatches, "validations": validations}


def write_plan(path: Path, report: dict[str, Any]) -> None:
    atomic_write_json(path.expanduser().resolve(), report)


def consolidate_plans(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Globally revalidate and merge dry-run reports into one executable plan."""
    if not reports:
        raise ValueError("at least one report is required")
    operations = [copy.deepcopy(op) for report in reports for op in report.get("operations", [])]
    sources = [op["source"] for op in operations]
    if len(sources) != len(set(sources)):
        raise RuntimeError("a source is accounted more than once across reports")
    by_destination: dict[str, list[dict[str, Any]]] = {}
    for op in operations:
        if op.get("destination") and op["classification"] in {"move", "quarantine", "reuse_duplicate"}:
            by_destination.setdefault(op["destination"], []).append(op)
    for destination_text, group in by_destination.items():
        if len(group) < 2:
            continue
        hashes = [_sha256(Path(op["source"])) for op in group]
        if len(set(hashes)) == 1:
            mover = next((op for op in group if op["classification"] in {"move", "quarantine"}), None)
            if mover is not None:
                for op, digest in zip(group, hashes):
                    if op is not mover:
                        op.update(classification="reuse_duplicate", source_hash=digest, target_hash=digest, depends_on_planned=True, reason="global identical duplicate")
            continue
        destination = Path(destination_text)
        canonical: int | None = None
        if "/items/bilibili/BV1zUh56RE8k_p1/source/" in destination.as_posix():
            canonical = next((i for i, op in enumerate(group) if "/obsidian_本地知识库/" in op["source"]), None)
        elif destination.as_posix().endswith("/items/bilibili/BV11X4y1j7si_p4/source/metadata.json"):
            canonical = next((i for i, op in enumerate(group) if "/obsidian_本地知识库/" in op["source"]), None)
        if canonical is None:
            for op in group: op.update(classification="conflict", reason="unresolved global destination collision")
            continue
        group[canonical].update(classification="move", reason="globally audited canonical source")
        for index, (op, digest) in enumerate(zip(group, hashes)):
            if index == canonical: continue
            op.update(classification="move", destination=str(destination.parent / "legacy-variants" / digest[:12] / destination.name), reason="globally audited non-canonical variant preserved")
    counts = {name: sum(op.get("classification") == name for op in operations) for name in CLASSIFICATIONS}
    counts["unclassified"] = sum(op.get("classification") not in CLASSIFICATIONS for op in operations)
    counts["source_files"] = len(operations); counts["accounted_percent"] = 100.0
    packages: dict[tuple[str, str], dict[str, Any]] = {}
    for report in reports:
        for descriptor in report.get("packages") or []:
            packages[(descriptor["platform"], descriptor["item_id"])] = descriptor
    roots = [report["source_root"] for report in reports]
    library_root = reports[0]["library_root"]
    devices = {int(report["devices"]["source"]) for report in reports} | {int(report["devices"]["library"]) for report in reports}
    if len(devices) != 1: raise RuntimeError("consolidated reports cross devices")
    return sanitize({"schema_version": 1, "created_ns": time.time_ns(), "source_root": roots[0], "source_roots": roots,
                     "library_root": library_root, "devices": {"source": next(iter(devices)), "library": next(iter(devices)), "same_device": True},
                     "packages": list(packages.values()), "operations": operations, "summary": counts,
                     "input_report_count": len(reports), "unique_sources": len(set(sources))})
