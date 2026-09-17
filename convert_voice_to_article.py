#!/usr/bin/env python3
"""把有权访问的小鹅通视频下载并转换成适合交给 AI 的本地文章稿。"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import threading
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from output_paths import platform_output_root


@dataclass
class MediaCandidate:
    url: str
    referer: str = field(repr=False)
    page_url: str
    title: str
    score: int
    headers: dict[str, str] = field(default_factory=dict, repr=False)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def safe_filename(value: str) -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value).strip(" .")
    return value[:100] or "xet_video"


def display_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.query:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?…"
    return value[:180]


def video_directory(root: Path, title: str) -> Path:
    """每个视频独立目录；目录名使用页面标题的安全文件名。"""
    return root / safe_filename(title)


def page_title(page, fallback: str) -> str:
    try:
        title = page.title().strip()
    except Exception:
        title = ""
    return title or fallback


def media_score(url: str) -> int:
    """Prefer high quality playlists, but accept any signed/valid m3u8."""
    lowered = url.lower()
    score = 0
    if "v.f421220" in lowered:
        score += 1000
    for token, points in (("2160", 240), ("1440", 200), ("1080", 160), ("720", 120), ("540", 90), ("480", 60)):
        if token in lowered:
            score += points
    if "master" in lowered or "playlist" in lowered:
        score += 20
    if "audio" in lowered or "-a.m3u8" in lowered:
        score -= 100
    if "sign=" in lowered or "token=" in lowered or "auth" in lowered:
        score += 10
    return score


def capture_media(url: str, session_dir: Path, wait_seconds: int, headless: bool, cdp_url: str | None = None) -> list[MediaCandidate]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright。请先执行：uv sync && uv run playwright install chromium") from exc

    candidates: dict[str, MediaCandidate] = {}
    fallback_title = Path(urlparse(url).path).name or "xet_video"

    with sync_playwright() as playwright:
        cdp_url = cdp_url or os.environ.get("VIDEO_EXTRACT_CDP_URL")
        attached = bool(cdp_url)
        if attached:
            browser = playwright.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context(viewport={"width": 1280, "height": 800})
        else:
            session_dir.mkdir(parents=True, exist_ok=True)
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(session_dir),
                headless=headless,
                args=["--no-sandbox"],
                viewport={"width": 1280, "height": 800},
            )
        page = next((p for p in context.pages if "xet." in p.url or "xiaoe" in p.url), None)
        page = page or (context.pages[0] if context.pages else context.new_page())

        def on_request(request) -> None:
            request_url = request.url
            if ".m3u8" not in request_url.lower():
                return
            candidate = MediaCandidate(
                url=request_url,
                referer=request.headers.get("referer", url),
                page_url=page.url,
                title=page_title(page, fallback_title),
                score=media_score(request_url),
                headers={str(key): str(value) for key, value in request.headers.items()},
            )
            old = candidates.get(request_url)
            if old is None or candidate.score > old.score:
                candidates[request_url] = candidate
                print(f"捕获到媒体地址（评分 {candidate.score}）：{display_url(candidate.url)}")

        page.on("request", on_request)
        try:
            print(f"打开页面：{url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            print(f"浏览器已打开，最多等待 {wait_seconds} 秒。首次运行请在浏览器中完成登录。")
            # 播放器通常是异步挂载的；如果只在等待结束后检查一次，
            # video 元素可能尚未出现，导致没有触发 m3u8 请求。
            # 在整个等待窗口内反复尝试点击/播放，兼容自动播放被浏览器拦截的情况。
            for _ in range(max(wait_seconds, 1)):
                if candidates:
                    # m3u8 已捕获；多等一秒给同页面的高清变体请求机会。
                    page.wait_for_timeout(1000)
                    break
                try:
                    video = page.locator("video").first
                    video.wait_for(state="attached", timeout=1000)
                    video.click(position={"x": 20, "y": 20}, timeout=2_000, force=True)
                    page.wait_for_timeout(1000)
                except Exception:
                    page.wait_for_timeout(1000)
        finally:
            if not attached:
                context.close()

    return sorted(candidates.values(), key=lambda item: item.score, reverse=True)


def _find_subtitle_urls(value, found: set[str] | None = None) -> set[str]:
    """从 B 站播放器响应中找字幕 JSON 地址，不读取系统 Chrome Cookie。"""
    if found is None:
        found = set()
    if isinstance(value, dict):
        for key in ("subtitle_url", "subtitleUrl"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://", "//")):
                found.add("https:" + candidate if candidate.startswith("//") else candidate)
        for child in value.values():
            _find_subtitle_urls(child, found)
    elif isinstance(value, list):
        for child in value:
            _find_subtitle_urls(child, found)
    return found


def _find_subtitle_rows(value, rows: list[dict] | None = None) -> list[dict]:
    if rows is None:
        rows = []
    if isinstance(value, dict):
        if {"from", "to", "content"}.issubset(value):
            try:
                rows.append({"start": float(value["from"]), "end": float(value["to"]), "text": str(value["content"]).strip()})
            except (TypeError, ValueError):
                pass
        for child in value.values():
            _find_subtitle_rows(child, rows)
    elif isinstance(value, list):
        for child in value:
            _find_subtitle_rows(child, rows)
    return rows


def capture_bilibili_subtitles(url: str, session_dir: Path, wait_seconds: int, headless: bool) -> tuple[str, list[dict]]:
    """在独立 Playwright 浏览器中登录并提取 B 站正式字幕轨道。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright。请先执行：uv sync && uv run playwright install chromium") from exc

    subtitle_urls: set[str] = set()
    title_fallback = Path(urlparse(url).path).name or "bilibili_video"
    with sync_playwright() as playwright:
        session_dir.mkdir(parents=True, exist_ok=True)
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(session_dir),
            headless=headless,
            args=["--no-sandbox"],
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else context.new_page()

        def on_response(response) -> None:
            response_url = response.url.lower()
            if not any(token in response_url for token in ("subtitle", "player/wbi", "player.so")):
                return
            try:
                subtitle_urls.update(_find_subtitle_urls(response.json()))
            except Exception:
                pass

        page.on("response", on_response)
        try:
            print(f"打开 B 站页面：{url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            print(f"独立浏览器已打开，最多等待 {wait_seconds} 秒。请在窗口中扫码登录 B 站并播放视频。")
            for _ in range(max(wait_seconds, 1)):
                page.wait_for_timeout(1000)
                if subtitle_urls:
                    page.wait_for_timeout(1500)
                    break

            title = page_title(page, title_fallback).replace("_哔哩哔哩_bilibili", "").strip()
            rows: list[dict] = []
            for subtitle_url in subtitle_urls:
                try:
                    response = context.request.get(subtitle_url, headers={"Referer": page.url})
                    if response.ok:
                        rows = _find_subtitle_rows(response.json())
                        if rows:
                            break
                except Exception as exc:
                    print(f"字幕请求失败：{exc}")
            return title, sorted({(row["start"], row["end"], row["text"]): row for row in rows}.values(), key=lambda row: row["start"])
        finally:
            context.close()


def download_video(candidate: MediaCandidate, output_dir: Path) -> Path:
    if not command_exists("ffmpeg"):
        raise RuntimeError("找不到 ffmpeg。请先执行：brew install ffmpeg")

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_filename(candidate.title)
    template = output_dir / f"{stem}.%(ext)s"
    command = [
        sys.executable, "-m", "yt_dlp",
        "--referer", candidate.referer or candidate.page_url,
        "--merge-output-format", "mp4",
        "--concurrent-fragments", "5",
        "-o", str(template),
        candidate.url,
    ]
    print(f"开始下载：{candidate.title}")
    subprocess.run(command, check=True)

    video_suffixes = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".ts"}
    files = sorted(
        (path for path in output_dir.glob(f"{stem}.*") if path.suffix.lower() in video_suffixes),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise RuntimeError(f"yt-dlp 执行完成，但没有在 {output_dir} 找到视频文件")
    return files[0]


def extract_audio(video_path: Path, output_dir: Path) -> Path:
    if not command_exists("ffmpeg"):
        raise RuntimeError("找不到 ffmpeg。请先执行：brew install ffmpeg")
    audio_path = output_dir / f"{video_path.stem}.wav"
    command = [
        "ffmpeg", "-y", "-i", str(video_path), "-vn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio_path),
    ]
    print(f"提取音频：{audio_path}")
    subprocess.run(command, check=True)
    return audio_path


def format_timestamp(seconds: float, decimal: str = ",") -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{decimal}{millis:03d}"


def short_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


_WHISPER_LOCAL = threading.local()


def transcribe(audio_path: Path, output_dir: Path, model_name: str, language: str | None, task: str) -> tuple[list[dict], str | None]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("缺少 faster-whisper。请先执行：uv sync") from exc

    cache_key = (model_name, language, task)
    cached = getattr(_WHISPER_LOCAL, "model", None)
    if not cached or cached[0] != cache_key:
        print(f"加载本地 Whisper 模型：{model_name}（当前 worker 内复用）")
        cached = (cache_key, WhisperModel(model_name, device="cpu", compute_type="int8"))
        _WHISPER_LOCAL.model = cached
    model = cached[1]
    segments, info = model.transcribe(
        str(audio_path),
        language=language,
        task=task,
        vad_filter=True,
        beam_size=5,
    )
    rows = []
    for segment in segments:
        text = segment.text.strip()
        if text:
            rows.append({"start": float(segment.start), "end": float(segment.end), "text": text})

    if not rows:
        raise RuntimeError("Whisper 没有识别出文字；请检查视频是否有音轨，或尝试更大的模型")
    detected_language = getattr(info, "language", None)
    return rows, detected_language


def write_outputs(segments: list[dict], output_dir: Path, title: str, source_url: str, model_name: str, task: str, language: str | None) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    txt_path = output_dir / "transcript.txt"
    srt_path = output_dir / "transcript.srt"
    article_path = output_dir / "article.md"
    metadata_path = output_dir / "metadata.json"

    full_text = "\n".join(item["text"] for item in segments)
    txt_path.write_text(full_text + "\n", encoding="utf-8")

    srt_blocks = []
    for index, item in enumerate(segments, 1):
        srt_blocks.append(
            f"{index}\n{format_timestamp(item['start'])} --> {format_timestamp(item['end'])}\n{item['text']}\n"
        )
    srt_path.write_text("\n".join(srt_blocks), encoding="utf-8")

    paragraphs: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0
    for item in segments:
        gap = item["start"] - current[-1]["end"] if current else 0
        if current and (current_chars >= 280 or gap >= 2.5 or item["start"] - current[0]["start"] >= 50):
            paragraphs.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += len(item["text"])
    if current:
        paragraphs.append(current)

    body = []
    for paragraph in paragraphs:
        start = paragraph[0]["start"]
        text = "".join(item["text"] for item in paragraph).strip()
        body.append(f"**[{short_timestamp(start)}]** {text}")

    article = [
        f"# {title}",
        "",
        "> 这是本地 Whisper 自动转写并按时间段整理的文章草稿，尚未进行事实校对。",
        "",
        f"- 来源：{source_url}",
        f"- 模型：`{model_name}`",
        f"- 任务：`{task}`（`translate` 会由 Whisper 本地翻译为英文）",
        f"- 语言：`{language or 'auto'}`",
        "",
        "## 转写正文",
        "",
        "\n\n".join(body),
        "",
    ]
    article_path.write_text("\n".join(article), encoding="utf-8")

    metadata = {
        "title": title,
        "source_url": source_url,
        "model": model_name,
        "task": task,
        "language": language,
        "created_at": datetime.now().astimezone().isoformat(),
        "segment_count": len(segments),
        "files": {"transcript": txt_path.name, "srt": srt_path.name, "article": article_path.name},
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"txt": txt_path, "srt": srt_path, "article": article_path, "metadata": metadata_path}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", help="视频页面 URL；如果使用 --video 可省略")
    parser.add_argument("--bilibili", action="store_true", help="使用独立浏览器提取 B 站正式字幕，不读取 Chrome Cookie")
    parser.add_argument("--video", type=Path, help="跳过下载，直接处理一个本地视频")
    parser.add_argument("--source-url", help="使用 --video 时写入文章元数据的原始页面 URL")
    parser.add_argument("--output-dir", type=Path, help="输出根目录；默认按平台写入 Obsidian Vault")
    parser.add_argument("--browser-session", type=Path, default=Path("work/browser_session"))
    parser.add_argument("--bilibili-session", type=Path, default=Path("work/bilibili_browser_session"))
    parser.add_argument("--wait-seconds", type=int, default=30, help="浏览器打开后等待时间，默认 30 秒")
    parser.add_argument("--headless", action="store_true", help="不显示浏览器；首次登录不要使用")
    parser.add_argument("--cdp-url", help="连接已打开的 Chrome CDP 地址，例如 http://127.0.0.1:9222；也可用 VIDEO_EXTRACT_CDP_URL")
    parser.add_argument("--model", default="small", help="Whisper 模型名，如 tiny/base/small/medium 或本地模型目录")
    parser.add_argument("--language", default="zh", help="音频语言；自动检测可用 --language auto")
    parser.add_argument("--task", choices=["transcribe", "translate"], default="transcribe")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.video is None and not args.url:
        print("请提供视频页面 URL，或使用 --video 指定本地视频", file=sys.stderr)
        return 2
    if args.task == "translate" and args.language == "auto":
        print("提示：translate 会将识别结果翻译为英文；建议显式指定 --language zh 等源语言。")
    language = None if args.language == "auto" else args.language
    if args.output_dir is None:
        args.output_dir = platform_output_root("bilibili" if args.bilibili else "xiaoe")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.bilibili:
        if not args.url or args.video:
            print("B 站字幕模式需要直接提供 URL，不能和 --video 一起使用", file=sys.stderr)
            return 2
        title, segments = capture_bilibili_subtitles(args.url, args.bilibili_session, args.wait_seconds, args.headless)
        if not segments:
            print("没有找到正式字幕。请确认已登录、视频可播放，并增加 --wait-seconds；弹幕不属于正式字幕。", file=sys.stderr)
            return 1
        output_dir = video_directory(args.output_dir, title)
        source_dir = output_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        outputs = write_outputs(segments, source_dir, title, args.url, "bilibili subtitle", "subtitle", language)
        print("B 站字幕提取完成：")
        for path in outputs.values():
            print(f"  {path.resolve()}")
        return 0

    if args.video:
        video_path = args.video.expanduser().resolve()
        if not video_path.exists():
            print(f"找不到本地视频：{video_path}", file=sys.stderr)
            return 2
        source_url = args.source_url or "local file"
        title = video_path.stem
        output_dir = video_directory(args.output_dir, title)
        output_dir.mkdir(parents=True, exist_ok=True)
    else:
        candidates = capture_media(args.url, args.browser_session, args.wait_seconds, args.headless, args.cdp_url)
        if not candidates:
            print("没有捕获到 m3u8。请确认已在浏览器登录并手动播放视频，然后增加 --wait-seconds。", file=sys.stderr)
            return 1
        best = candidates[0]
        output_dir = video_directory(args.output_dir, best.title)
        output_dir.mkdir(parents=True, exist_ok=True)
        capture_path = output_dir / "captured_media.json"
        capture_path.write_text(json.dumps([{
            "page_url": display_url(item.page_url), "title": item.title,
            "score": item.score, "media": display_url(item.url),
        } for item in candidates], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"选用媒体地址（评分 {best.score}）：{display_url(best.url)}")
        video_path = download_video(best, output_dir)
        source_url = args.url
        title = best.title

    audio_path = extract_audio(video_path, output_dir)
    segments, detected_language = transcribe(audio_path, output_dir, args.model, language, args.task)
    outputs = write_outputs(segments, output_dir, title, source_url, args.model, args.task, detected_language or language)
    print("处理完成：")
    for path in [video_path, audio_path, *outputs.values()]:
        print(f"  {path.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130)
    except subprocess.CalledProcessError as exc:
        print(f"外部命令失败（退出码 {exc.returncode}）：{exc.cmd}", file=sys.stderr)
        raise SystemExit(exc.returncode or 1)
    except Exception as exc:
        print(f"失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
