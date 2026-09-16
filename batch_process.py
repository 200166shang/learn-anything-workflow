#!/usr/bin/env python3
"""按课程清单批量下载并转写视频，每个视频一个目录。"""

from __future__ import annotations

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from convert_voice_to_article import (
    MediaCandidate,
    capture_media,
    download_video,
    extract_audio,
    safe_filename,
    transcribe,
    video_directory,
    write_outputs,
)
from output_paths import platform_output_root


VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".ts"}


def existing_video(video_dir: Path) -> Path | None:
    files = sorted(
        (path for path in video_dir.iterdir() if path.suffix.lower() in VIDEO_SUFFIXES),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ) if video_dir.exists() else []
    return files[0] if files else None


def is_complete(video_dir: Path) -> bool:
    video = existing_video(video_dir)
    return bool(video and all((video_dir / name).exists() for name in ("transcript.txt", "transcript.srt", "article.md", "metadata.json")))


class Manifest:
    def __init__(self, path: Path, course_url: str, items: list[dict]):
        self.path = path
        self.lock = threading.Lock()
        self.data = {
            "schema_version": 1,
            "started_at": datetime.now().astimezone().isoformat(),
            "course_url": course_url,
            "section": "第七章-机器人传感器接入",
            "total": len(items),
            "items": {
                str(item["video_id"]): {
                    "index": item["index"],
                    "title": item["title"],
                    "url": item["url"],
                    "status": "pending",
                }
                for item in items
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.flush()

    def update(self, item_id: str, **values) -> None:
        with self.lock:
            self.data["items"][item_id].update(values)
            self.flush()

    def flush(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def process_one(item: dict, candidate: MediaCandidate | None, output_root: Path, model: str, language: str, task: str, manifest: Manifest) -> str:
    item_id = str(item["video_id"])
    output_dir = video_directory(output_root, item["title"])
    output_dir.mkdir(parents=True, exist_ok=True)
    if is_complete(output_dir):
        manifest.update(item_id, status="skipped", output_dir=str(output_dir))
        return f"跳过（已完成）：{item['index']} {item['title']}"

    try:
        manifest.update(item_id, status="processing", output_dir=str(output_dir))
        video_path = existing_video(output_dir)
        if video_path is None:
            if candidate is None:
                raise RuntimeError("没有可用的媒体地址")
            video_path = download_video(candidate, output_dir)
        audio_path = extract_audio(video_path, output_dir)
        segments, detected_language = transcribe(audio_path, output_dir, model, None if language == "auto" else language, task)
        write_outputs(segments, output_dir, item["title"], item["url"], model, task, detected_language or language)
        manifest.update(item_id, status="completed", segment_count=len(segments))
        return f"完成：{item['index']} {item['title']}"
    except Exception as exc:
        manifest.update(item_id, status="failed", error=str(exc))
        return f"失败：{item['index']} {item['title']}：{exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_course_root = platform_output_root("xiaoe") / "小沫ROS智能体机器人课程"
    parser.add_argument("--catalog", type=Path, default=default_course_root / "course_catalog.json")
    parser.add_argument("--output-dir", type=Path, default=default_course_root)
    parser.add_argument("--browser-session", type=Path, default=Path("work/browser_session"))
    parser.add_argument("--manifest", type=Path, default=default_course_root / "chapter_7_manifest.json")
    parser.add_argument("--wait-seconds", type=int, default=15)
    parser.add_argument("--workers", type=int, default=2, help="下载/转写并行数，默认 2")
    parser.add_argument("--model", default="base")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--task", choices=["transcribe", "translate"], default="transcribe")
    parser.add_argument("--section", default="第七章-机器人传感器接入")
    parser.add_argument("--visible", action="store_true", help="显示浏览器；首次登录时使用")
    parser.add_argument("--limit", type=int, help="只处理前 N 个视频，用于试跑")
    args = parser.parse_args()

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    items = [
        item for item in catalog["lessons"]
        if item.get("section") == args.section and item.get("is_video")
    ]
    if args.limit:
        items = items[:args.limit]
    if not items:
        raise SystemExit("清单中没有找到第七章视频")

    print(f"准备处理第七章：{len(items)} 个视频；并行数：{max(1, args.workers)}")
    manifest = Manifest(args.manifest, catalog["course_url"], items)
    manifest.data["section"] = args.section
    manifest.flush()

    # 同一持久化浏览器目录不能被多个 Chromium 进程同时打开，因此捕获阶段串行。
    candidates: dict[str, MediaCandidate] = {}
    for number, item in enumerate(items, 1):
        output_dir = video_directory(args.output_dir, item["title"])
        if is_complete(output_dir):
            print(f"[{number}/{len(items)}] 跳过已完成：{item['title']}")
            manifest.update(str(item["video_id"]), status="skipped", output_dir=str(output_dir))
            continue
        if existing_video(output_dir) is not None:
            print(f"[{number}/{len(items)}] 使用已有视频：{item['title']}")
            continue
        print(f"[{number}/{len(items)}] 捕获：{item['title']}")
        captured = capture_media(item["url"], args.browser_session, args.wait_seconds, not args.visible)
        if not captured:
            manifest.update(str(item["video_id"]), status="failed", error="未捕获到 m3u8")
            print(f"  捕获失败：{item['title']}")
            continue
        best = captured[0]
        # 目录名和文章标题以课程清单为准，避免页面标题变化导致目录漂移。
        candidates[str(item["video_id"])] = MediaCandidate(
            url=best.url,
            referer=best.referer,
            page_url=item["url"],
            title=item["title"],
            score=best.score,
        )

    pending = [item for item in items if str(item["video_id"]) in candidates or existing_video(video_directory(args.output_dir, item["title"]))]
    print(f"开始并行处理：{len(pending)} 个视频")
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [
            executor.submit(
                process_one,
                item,
                candidates.get(str(item["video_id"])),
                args.output_dir,
                args.model,
                args.language,
                args.task,
                manifest,
            )
            for item in pending
        ]
        for future in as_completed(futures):
            print(future.result())

    completed = sum(value["status"] == "completed" for value in manifest.data["items"].values())
    skipped = sum(value["status"] == "skipped" for value in manifest.data["items"].values())
    failed = sum(value["status"] == "failed" for value in manifest.data["items"].values())
    print(f"批处理结束：完成 {completed}，跳过 {skipped}，失败 {failed}")
    print(f"进度清单：{args.manifest.resolve()}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
