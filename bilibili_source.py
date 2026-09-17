#!/usr/bin/env python3
"""B 站素材获取公共逻辑：缓存视频、查找已有素材。"""

from __future__ import annotations

import json
import hashlib
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from convert_voice_to_article import MediaCandidate, capture_media


def source_identity(source_url: str) -> dict[str, str]:
    """Return a stable Bilibili item identity without retaining tracking parameters."""
    parsed = urlparse(source_url)
    match = re.search(r"/video/(BV[0-9A-Za-z]+|av\d+)", parsed.path, re.IGNORECASE)
    if match:
        item_id = match.group(1)
        page = parse_qs(parsed.query).get("p", ["1"])[0]
        return {"kind": "platform_id", "value": f"{item_id}:p{page}"}
    fingerprint = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:20]
    return {"kind": "source_fingerprint", "value": fingerprint}


def find_cached_output(root: Path, source_url: str) -> Path | None:
    """按 metadata 中的原始 URL 找到已有视频目录，避免重复下载。"""
    if not root.exists():
        return None
    expected_identity = source_identity(source_url)
    for metadata_path in root.rglob("metadata.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        stored_url = metadata.get("source_url")
        if not isinstance(stored_url, str):
            continue
        if stored_url != source_url and source_identity(stored_url) != expected_identity:
            continue
        output_dir = metadata_path.parent.parent if metadata_path.parent.name == "source" else metadata_path.parent
        source_dir = output_dir / "source"
        if any((path.exists() for path in (
            source_dir / "source.mp4",
            source_dir / "transcript.srt",
            output_dir / "source.mp4",
            output_dir / "transcript.srt",
        ))):
            return output_dir
    return None


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


def download_cached_video(
    url: str,
    title: str,
    session_dir: Path,
    wait_seconds: int,
    headless: bool,
    output_dir: Path,
    quality: int,
) -> Path:
    """下载并缓存为固定的 source/source.mp4。"""
    if not shutil_which("ffmpeg"):
        raise RuntimeError("找不到 ffmpeg。请先执行：brew install ffmpeg")
    output_dir.mkdir(parents=True, exist_ok=True)
    source_path = output_dir / "source.mp4"
    if source_path.exists() and source_path.stat().st_size > 0:
        return source_path
    for old in output_dir.glob("source.*"):
        if old.is_file() and old.name != "source.mp4":
            old.unlink(missing_ok=True)
    template = output_dir / "source.%(ext)s"
    # B 站竖屏视频的 height 可能是 820（480x820）；同时按宽度兜底，避免误判无可用格式。
    format_selector = f"bv*[height<={quality}]+ba/bv*[width<={quality}]+ba/b[height<={quality}]/b[width<={quality}]/b"
    common = [
        sys.executable, "-m", "yt_dlp", "--no-playlist", "--format", format_selector,
        "--merge-output-format", "mp4", "--remux-video", "mp4", "--concurrent-fragments", "5",
        "-o", str(template),
    ]
    print(f"下载并缓存 B 站视频（最高 {quality}p）：{title}")
    try:
        subprocess.run([*common, "--referer", url, url], check=True)
    except subprocess.CalledProcessError:
        print("直接下载失败，切换到浏览器捕获媒体地址兜底", file=sys.stderr)
        media_candidates = capture_media(url, session_dir, wait_seconds, headless)
        if not media_candidates:
            raise RuntimeError("没有捕获到 B 站媒体地址；请确认已登录并播放视频")
        candidate: MediaCandidate = media_candidates[0]
        subprocess.run([
            *common, "--referer", candidate.referer or candidate.page_url, candidate.url,
        ], check=True)
    if source_path.exists():
        return source_path
    media_files = sorted(
        (path for path in output_dir.glob("source.*") if path.suffix.lower() in {".mkv", ".webm", ".mov", ".m4v", ".ts"}),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if media_files:
        raise RuntimeError(f"下载完成但没有得到 source.mp4：{media_files[0]}")
    raise RuntimeError("yt-dlp 执行完成，但没有找到 source.mp4")
