"""Derived, video-only playback views for local media players."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any


def safe_name(value: str) -> str:
    value = re.sub(r"[/:\\\x00]", "-", value).strip(" .")
    return value or "未命名"


def _find_video(lesson: dict[str, Any], source_root: Path, library_root: Path) -> Path | None:
    identity = str(lesson.get("video_id") or "")
    for platform_root in sorted((library_root / "items").glob("*")):
        for relative in ("source/source.mp4", "media/video.mp4"):
            candidate = platform_root / identity / relative
            if candidate.is_file():
                return candidate.resolve()
    title = str(lesson.get("title") or "")
    title_dir = source_root / safe_name(title)
    exact = title_dir / f"{safe_name(title)}.mp4"
    if exact.is_file():
        return exact.resolve()
    matches = sorted(title_dir.glob("*.mp4")) if title_dir.is_dir() else []
    return matches[0].resolve() if len(matches) == 1 else None


def _replace_derived_directory(staged: Path, destination: Path) -> None:
    if destination.exists():
        for path in destination.iterdir():
            if not path.is_symlink():
                raise RuntimeError(f"refusing to replace playback directory containing a real file: {path}")
        shutil.rmtree(destination)
    os.replace(staged, destination)


def build_xiaoe_playback_views(
    catalog_path: Path,
    source_root: Path,
    library_root: Path,
    output_root: Path,
    sections: list[str] | None = None,
) -> dict[str, Any]:
    catalog_path = catalog_path.expanduser().resolve()
    source_root = source_root.expanduser().resolve()
    library_root = library_root.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    course = safe_name(str(catalog.get("course_title") or catalog_path.parent.name))
    requested = set(sections or [])
    available = {str(x.get("section") or "未分章") for x in catalog.get("lessons", []) if x.get("is_video")}
    unknown = requested - available
    if unknown:
        raise ValueError(f"unknown section: {', '.join(sorted(unknown))}")
    selected = requested or available
    course_root = output_root / "xiaoe" / course
    course_root.mkdir(parents=True, exist_ok=True)
    results = []
    for section in sorted(selected, key=lambda name: min(int(x.get("sort_value") or x.get("index") or 0) for x in catalog["lessons"] if x.get("section") == name)):
        lessons = sorted(
            (x for x in catalog["lessons"] if x.get("is_video") and x.get("section") == section),
            key=lambda x: int(x.get("sort_value") or x.get("index") or 0),
        )
        section_root = course_root / safe_name(section)
        section_root.mkdir(parents=True, exist_ok=True)
        destination = section_root / "播放目录"
        staged = Path(tempfile.mkdtemp(prefix=".播放目录-", dir=section_root))
        found, missing = [], []
        try:
            for position, lesson in enumerate(lessons, 1):
                video = _find_video(lesson, source_root, library_root)
                if not video:
                    missing.append({"video_id": lesson.get("video_id"), "title": lesson.get("title")})
                    continue
                filename = f"{position:03d} {safe_name(str(lesson.get('title') or video.stem))}.mp4"
                link = staged / filename
                link.symlink_to(video)
                found.append({"position": position, "title": lesson.get("title"), "video_id": lesson.get("video_id"), "link": filename, "source": str(video)})
            _replace_derived_directory(staged, destination)
        except Exception:
            if staged.exists():
                shutil.rmtree(staged)
            raise
        playlist = section_root / "章节播放列表.m3u8"
        content = "#EXTM3U\n" + "".join(f"#EXTINF:-1,{item['title']}\n播放目录/{item['link']}\n" for item in found)
        playlist.write_text(content, encoding="utf-8")
        report = {"section": section, "video_count": len(found), "missing_count": len(missing), "playback_directory": str(destination), "playlist": str(playlist), "videos": found, "missing": missing}
        (section_root / "播放清单.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        results.append(report)
    return {"ok": all(not x["missing"] for x in results), "course": catalog.get("course_title"), "output": str(course_root), "sections": results, "video_count": sum(x["video_count"] for x in results), "missing_count": sum(x["missing_count"] for x in results)}


def build_playback_views(catalog_path: Path, library_root: Path, output_root: Path, sections: list[str] | None = None) -> dict[str, Any]:
    catalog_path = catalog_path.expanduser().resolve(); library_root = library_root.expanduser().resolve()
    catalog = json.loads(catalog_path.read_text(encoding="utf-8")); platform = str(catalog.get("platform") or catalog_path.parent.parent.name)
    converted = dict(catalog); converted["course_title"] = catalog.get("title") or catalog.get("course_title") or catalog.get("collection_title") or catalog_path.parent.name
    raw_items = catalog.get("items") or catalog.get("lessons") or []
    converted["lessons"] = []
    skipped = 0
    for position, item in enumerate(raw_items, 1):
        identity = item.get("id") or item.get("video_id")
        if not identity and platform == "bilibili" and catalog.get("bvid"): identity = f"{catalog['bvid']}_p{item.get('index') or position}"
        lesson = {"index": item.get("index") or item.get("ordinal") or position, "sort_value": item.get("sort_value") or item.get("index") or item.get("ordinal") or position, "title": item.get("title"), "video_id": identity, "is_video": item.get("is_video", True), "section": item.get("section") or "未分章"}
        if lesson["is_video"] and _find_video(lesson, Path("/__no_legacy_source__"), library_root): converted["lessons"].append(lesson)
        elif lesson["is_video"]: skipped += 1
    temporary = catalog_path.parent / ".playback-catalog.json"
    temporary.write_text(json.dumps(converted, ensure_ascii=False), encoding="utf-8")
    try:
        result = build_xiaoe_playback_views(temporary, Path("/__no_legacy_source__"), library_root, output_root, sections)
        old = Path(result["output"]); desired = output_root.expanduser().resolve() / platform / safe_name(str(converted["course_title"]))
        if old != desired:
            desired.parent.mkdir(parents=True, exist_ok=True); os.replace(old, desired); result["output"] = str(desired)
            for section in result["sections"]:
                section_root = desired / safe_name(section["section"]); section["playback_directory"] = str(section_root / "播放目录"); section["playlist"] = str(section_root / "章节播放列表.m3u8")
        result["catalog_missing_or_unreadable"] = skipped
        return result
    finally:
        temporary.unlink(missing_ok=True)


def verify_playback(output_root: Path) -> dict[str, Any]:
    broken = non_mp4 = real_files = 0; directories = 0
    for directory in output_root.expanduser().resolve().rglob("播放目录"):
        if not directory.is_dir(): continue
        directories += 1
        for path in directory.iterdir():
            if path.suffix.lower() != ".mp4": non_mp4 += 1
            if not path.is_symlink(): real_files += 1
            elif not path.exists(): broken += 1
    return {"ok": broken == 0 and non_mp4 == 0 and real_files == 0, "playback_directories": directories, "broken_symlinks": broken, "non_mp4_entries": non_mp4, "real_files": real_files}
