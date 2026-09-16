#!/usr/bin/env python3
"""并行预缓存 B 站视频，不抽帧、不生成审阅页和学习笔记。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from bilibili_source import download_cached_video, find_cached_output, source_identity
from convert_voice_to_article import safe_filename
from output_paths import platform_output_root
from video_extract.contracts import SCHEMA_VERSION
from video_extract.manifest import atomic_write_json
from video_extract.validate import validate_item


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls_file", type=Path, help="每行一个 B 站视频 URL 的文本文件")
    parser.add_argument("--output-dir", type=Path, default=platform_output_root("bilibili"))
    parser.add_argument("--session-dir", type=Path, default=Path("work/bilibili_browser_session"))
    parser.add_argument("--workers", type=int, default=3, help="并行下载数，默认 3")
    parser.add_argument("--quality", type=int, default=480, help="最高视频高度，默认 480p")
    parser.add_argument("--wait-seconds", type=int, default=15)
    parser.add_argument("--headless", action="store_true")
    return parser.parse_args()


def probe_title(url: str) -> str:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--no-playlist", "--get-title", "--skip-download", url],
            check=True, capture_output=True, text=True, timeout=60,
        )
        title = result.stdout.strip().splitlines()[0]
        if title:
            return title
    except (subprocess.SubprocessError, IndexError):
        pass
    return url.rstrip("/").split("/")[-1].replace("?", "_")


def write_source_manifest(package: Path, url: str, title: str, quality: int) -> None:
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "platform": "bilibili",
        "source_url": url,
        "title": title,
        "identity": source_identity(url),
        "status": "media_ready",
        "verified_gate": "media_ready",
        "next_action": "prepare_learning_package",
        "updated_at": datetime.now().astimezone().isoformat(),
        "video": "source/source.mp4",
        "subtitle": None,
        "quality_limit": f"{quality}p",
        "candidate_count": 0,
        "approved_count": 0,
        "artifacts": {
            "video": "source/source.mp4",
            "metadata": "source/metadata.json",
            "contact_sheet": "frames/contact_sheet.jpg",
            "candidate_json": "review/keyframes.json",
            "approved_json": "review/approved_keyframes.json",
            "review_html": "review/contact_sheet.html",
            "codex_review_prompt": "review/codex_review_prompt.md",
            "notes_input": "notes/notes_input.md",
            "notes": "notes/notes.md",
        },
    }
    atomic_write_json(package / "manifest.json", manifest)
    checked = validate_item(package)
    if "media_ready" not in checked.passed:
        raise RuntimeError("downloaded media failed validation: " + "; ".join(checked.missing + checked.invalid))


def download_one(url: str, args: argparse.Namespace) -> tuple[str, str]:
    root = args.output_dir.expanduser().resolve()
    cached = find_cached_output(root, url)
    if cached and (cached / "source/source.mp4").exists():
        return url, f"已存在：{cached}"

    title = probe_title(url)
    package = root / safe_filename(title)
    package.mkdir(parents=True, exist_ok=True)
    source_dir = package / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    download_cached_video(url, title, args.session_dir, args.wait_seconds, args.headless, source_dir, max(144, args.quality))
    metadata = {
        "platform": "bilibili",
        "source_url": url,
        "title": title,
        "captured_at": datetime.now().astimezone().isoformat(),
        "source": "prefetch_bilibili",
    }
    (source_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_source_manifest(package, url, title, max(144, args.quality))
    return url, f"完成：{package}"


def main() -> int:
    args = parse_args()
    urls = [line.strip() for line in args.urls_file.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not urls:
        print("URL 列表为空", file=sys.stderr)
        return 2
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.session_dir.mkdir(parents=True, exist_ok=True)
    workers = max(1, min(args.workers, len(urls)))
    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(download_one, url, args) for url in urls]
        for future in as_completed(futures):
            try:
                url, message = future.result()
                print(f"[{url}] {message}")
            except Exception as exc:
                failures += 1
                print(f"下载失败：{exc}", file=sys.stderr)
    print(f"批量预缓存完成：{len(urls) - failures}/{len(urls)} 成功")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
