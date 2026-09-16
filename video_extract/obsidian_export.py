"""Rebuild the small Obsidian reading view from verified Media packages."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from .manifest import atomic_write_json, read_json
from .validate import validate_goals


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _yaml(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _bases(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    table = """filters:\n  and:\n    - file.inFolder(this.file.folder)\n    - file.ext == \"md\"\nviews:\n  - type: table\n    name: 视频资源\n    order:\n      - file.name\n      - note.platform\n      - note.collection\n      - note.section\n      - note.topics\n      - note.status\n      - note.duration_seconds\n    sort:\n      - property: note.collection\n      - property: note.section\n      - property: note.ordinal\n"""
    cards = """filters:\n  and:\n    - file.inFolder(this.file.folder)\n    - file.ext == \"md\"\nviews:\n  - type: cards\n    name: 课程浏览\n    groupBy:\n      property: note.collection\n      direction: ASC\n    order:\n      - file.name\n      - note.collection\n      - note.section\n      - note.status\n      - note.topics\n"""
    (root / "视频资源.base").write_text(table, encoding="utf-8")
    (root / "课程浏览.base").write_text(cards, encoding="utf-8")


def export_package(package: Path, root: Path, *, allow_preserved_invalid: bool = False) -> dict[str, Any]:
    package = package.expanduser().resolve(); root = root.expanduser().resolve()
    validation = validate_goals(package, ["notes_zh"])
    if not validation.get("ok") and not allow_preserved_invalid:
        return {"ok": False, "status": "validation_failed", "package": str(package), "validation": validation}
    manifest = read_json(package / "manifest.json")
    artifacts = manifest.get("artifacts", {})
    source_note = package / str(artifacts.get("notes") or "notes/notes.md")
    if not source_note.is_file():
        return {"ok": False, "status": "no_readable_note", "package": str(package), "validation": validation}
    platform = str(manifest.get("platform") or "unknown")
    collection = str(manifest.get("collection") or manifest.get("collection_title") or "未分类")
    section = str(manifest.get("section") or "未分章")
    title = str(manifest.get("title") or manifest.get("identity") or package.name)
    item_id = str(manifest.get("identity") or package.name)
    destination_dir = root / platform / collection / section / f"{title}__{item_id}"
    destination = destination_dir / f"{title}.md"
    export_manifest_path = package / "export-manifest.json"
    previous = read_json(export_manifest_path) if export_manifest_path.is_file() else {}
    if destination.is_file() and not previous.get("export_hash"):
        return {"ok": False, "status": "conflict", "package": str(package), "note": str(destination), "reason": "unregistered existing reading note"}
    if destination.is_file() and previous.get("export_hash") and _hash(destination) != previous["export_hash"]:
        return {"ok": False, "status": "conflict", "package": str(package), "note": str(destination)}

    body = source_note.read_text(encoding="utf-8")
    body = re.sub(r"(?i)&(?:#x0*20|#0*32|nbsp);", " ", body)
    approved_path = package / str(artifacts.get("approved_json") or "review/approved_keyframes.json")
    approved = read_json(approved_path) if approved_path.is_file() else {"approved": []}
    images: list[tuple[Path, str]] = []
    for entry in approved.get("approved", []):
        source = package / str(entry.get("image", ""))
        if source.is_file():
            images.append((source, source.name))
            if validation.get("ok"):
                body = re.sub(r"(?:\.\./)*(?:frames/)?keyframes/" + re.escape(source.name), "images/" + source.name, body)
    if not validation.get("ok"):
        def rewrite_legacy_image(match: re.Match[str]) -> str:
            alt, target = match.group(1), match.group(2)
            if re.match(r"^[a-z][a-z0-9+.-]*://", target, flags=re.IGNORECASE):
                return match.group(0)
            inside_package = False
            for candidate in ((source_note.parent / target).resolve(), (package / target).resolve()):
                try:
                    candidate.relative_to(package)
                except ValueError:
                    continue
                inside_package = True
                if candidate.is_file() and candidate.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                    if all(existing != candidate for existing, _ in images):
                        images.append((candidate, candidate.name))
                    return f"![{alt}](images/{candidate.name})"
            if inside_package:
                return f"[缺失的历史图片：{Path(target).name}]"
            return match.group(0)

        body = re.sub(r"!\[([^]]*)\]\(([^)]+)\)", rewrite_legacy_image, body)
    source_video = package / str(artifacts.get("source_video") or artifacts.get("video") or "source/source.mp4")
    properties = {
        "item_id": manifest.get("identity") or package.name, "platform": platform,
        "package_relpath": str(package.relative_to(package.parents[2])) if len(package.parents) > 2 else package.name,
        "source_video_relpath": str(source_video.relative_to(package)) if source_video.is_relative_to(package) else None,
        "collection": collection, "section": section, "ordinal": manifest.get("ordinal"),
        "topics": manifest.get("topics") or [], "status": "verified" if validation.get("ok") else "legacy_preserved_invalid",
        "migration_status": "migrated", "validation_status": "valid_complete" if validation.get("ok") else "legacy_preserved_invalid",
        "duration_seconds": manifest.get("duration_seconds"), "package_path": str(package),
        "source_video": str(source_video), "updated": str(manifest.get("updated") or manifest.get("updated_at") or ""),
    }
    frontmatter = "---\n" + "".join(f"{key}: {_yaml(value)}\n" for key, value in properties.items()) + "---\n\n"
    rendered = frontmatter + body.rstrip() + f"\n\n[打开源视频](file://{source_video})\n"
    source_hash = hashlib.sha256(b"obsidian-export-v4\0" + source_note.read_bytes() + json.dumps(approved, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if destination.is_file() and previous.get("source_hash") == source_hash and _hash(destination) == previous.get("export_hash"):
        _bases(root)
        return {"ok": True, "status": "reused", "package": str(package), "note": str(destination)}
    destination_dir.mkdir(parents=True, exist_ok=True)
    images_dir = destination_dir / "images"; images_dir.mkdir(exist_ok=True)
    adopted = {name for _, name in images}
    for existing in images_dir.iterdir():
        if existing.is_file() and existing.name not in adopted:
            existing.unlink()
    for source, name in images:
        shutil.copy2(source, images_dir / name)
    destination.write_text(rendered, encoding="utf-8")
    atomic_write_json(export_manifest_path, {"package": str(package), "note": str(destination), "source_hash": source_hash, "export_hash": _hash(destination), "images": sorted(adopted)})
    _bases(root)
    return {"ok": True, "status": "exported", "package": str(package), "note": str(destination), "images": len(images)}


def export_all_verified(library_root: Path, root: Path) -> dict[str, Any]:
    packages = sorted(path.parent for path in (library_root / "items").glob("*/*/manifest.json"))
    results = [export_package(package, root, allow_preserved_invalid=True) for package in packages]
    conflicts = sum(item.get("status") == "conflict" for item in results)
    exported = sum(item.get("ok", False) for item in results)
    return {"ok": conflicts == 0, "exported": exported, "skipped": len(results) - exported - conflicts, "conflicts": conflicts, "verified": sum(x.get("ok") and x.get("status") in {"exported", "reused"} and validate_goals(Path(x["package"]), ["notes_zh"]).get("ok") for x in results), "preserved_invalid": sum(x.get("ok") and not validate_goals(Path(x["package"]), ["notes_zh"]).get("ok") for x in results), "results": results}
