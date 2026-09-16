#!/usr/bin/env python3
"""Download authorized YouTube video and/or an existing Chinese audio track."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from convert_voice_to_article import safe_filename
from output_paths import platform_output_root


AUDIO_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".webm"}
DEFAULT_AUDIO_LANGUAGES = ("zh-Hans", "zh-CN", "zh")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument("--media", choices=("video", "audio", "both"), default="both")
    parser.add_argument(
        "--audio-language",
        default=",".join(DEFAULT_AUDIO_LANGUAGES),
        help="comma-separated language priority; default: zh-Hans,zh-CN,zh",
    )
    parser.add_argument("--max-height", type=int, default=720, help="maximum video height; default: 720")
    parser.add_argument("--output-dir", type=Path, default=platform_output_root("youtube"))
    parser.add_argument(
        "--cookies-from-browser",
        choices=("chrome", "chromium", "edge", "firefox", "safari"),
        help="use an authorized local browser session when the public request is insufficient",
    )
    parser.add_argument("--inspect-only", action="store_true", help="show available audio languages without downloading")
    parser.add_argument("--force", action="store_true", help="replace requested media artifacts")
    return parser.parse_args()


def ydl_base_options(args: argparse.Namespace) -> dict[str, Any]:
    options: dict[str, Any] = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    if args.cookies_from_browser:
        options["cookiesfrombrowser"] = (args.cookies_from_browser,)
    return options


def extract_info(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("缺少 yt-dlp；请先在 /Users/syz/code/video-extract-core 执行 uv sync") from exc
    options = {**ydl_base_options(args), "skip_download": True}
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(args.url, download=False)
    if not isinstance(info, dict) or info.get("_type") == "playlist":
        raise RuntimeError("当前入口只接受单个 YouTube 视频 URL")
    return info


def normalized_language(value: str) -> str:
    return value.strip().replace("_", "-").casefold()


def select_audio_format(info: dict[str, Any], priorities: list[str]) -> tuple[dict[str, Any], list[str]]:
    formats = [
        item
        for item in info.get("formats") or []
        if isinstance(item, dict)
        and item.get("vcodec") == "none"
        and item.get("acodec") not in {None, "none"}
        and isinstance(item.get("language"), str)
    ]
    available = sorted({str(item["language"]) for item in formats}, key=str.casefold)
    for wanted in priorities:
        wanted_norm = normalized_language(wanted)
        matching = [
            item
            for item in formats
            if normalized_language(str(item["language"])) == wanted_norm
            or normalized_language(str(item["language"])).startswith(wanted_norm + "-")
        ]
        if matching:
            best = max(
                matching,
                key=lambda item: (
                    float(item.get("abr") or item.get("tbr") or 0),
                    int(item.get("filesize") or item.get("filesize_approx") or 0),
                ),
            )
            return best, available
    raise RuntimeError(
        "该视频没有请求的中文音轨（请求："
        + ", ".join(priorities)
        + "；可用："
        + (", ".join(available) if available else "未报告语言")
        + "）。此入口不会自行生成翻译配音。"
    )


def find_existing_package(root: Path, video_id: str) -> Path | None:
    if not root.is_dir():
        return None
    for manifest_path in root.glob("*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        identity = manifest.get("identity")
        if isinstance(identity, dict) and identity.get("value") == video_id:
            return manifest_path.parent
    return None


def requested_media(media: str) -> list[str]:
    return ["video", "audio"] if media == "both" else [media]


def download_format(args: argparse.Namespace, format_selector: str, output_template: Path) -> Path:
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("缺少 yt-dlp；请先执行 uv sync") from exc
    options = {
        **ydl_base_options(args),
        "format": format_selector,
        "outtmpl": str(output_template),
        "overwrites": True,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([args.url])
    prefix = output_template.name.split("%(", 1)[0]
    candidates = [path for path in output_template.parent.glob(prefix + "*") if path.is_file() and not path.name.endswith((".part", ".ytdl"))]
    if not candidates:
        raise RuntimeError(f"yt-dlp 未生成预期文件：{output_template}")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def run_ffmpeg(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("缺少 ffmpeg；请先安装 ffmpeg") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg 处理失败（退出码 {exc.returncode}）") from exc


def make_audio(source: Path, destination: Path) -> None:
    temporary = destination.with_name(destination.stem + ".partial" + destination.suffix)
    run_ffmpeg(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-vn", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(temporary),
        ]
    )
    temporary.replace(destination)


def make_video(video_source: Path, audio_source: Path, destination: Path) -> None:
    temporary = destination.with_name(destination.stem + ".partial" + destination.suffix)
    run_ffmpeg(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video_source), "-i", str(audio_source),
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart", str(temporary),
        ]
    )
    temporary.replace(destination)


def nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_manifest(package: Path) -> dict[str, Any]:
    path = package / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def sanitized_error(exc: Exception) -> str:
    return re.sub(r"https?://\S+", "[redacted-url]", str(exc))


def main() -> int:
    args = parse_args()
    priorities = [part.strip() for part in args.audio_language.split(",") if part.strip()]
    if not priorities:
        print("--audio-language 不能为空", file=sys.stderr)
        return 2
    if args.max_height < 144:
        print("--max-height 必须至少为 144", file=sys.stderr)
        return 2

    package: Path | None = None
    failure_context: dict[str, Any] = {}
    try:
        info = extract_info(args)
        audio_format, available_languages = select_audio_format(info, priorities)
        video_id = str(info.get("id") or "").strip()
        title = str(info.get("title") or video_id or "youtube-video").strip()
        if not video_id:
            raise RuntimeError("YouTube 元数据缺少稳定视频 ID")
        inspection = {
            "platform": "youtube",
            "id": video_id,
            "title": title,
            "original_language": info.get("language"),
            "available_audio_languages": available_languages,
            "selected_audio_language": audio_format.get("language"),
            "selected_audio_format_id": audio_format.get("format_id"),
            "selected_audio_note": audio_format.get("format_note"),
        }
        if args.inspect_only:
            print(json.dumps(inspection, ensure_ascii=False, indent=2))
            return 0

        root = args.output_dir.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        package = find_existing_package(root, video_id) or root / f"{safe_filename(title)}__{video_id}"
        source_dir = package / "source"
        work_dir = source_dir / ".download-work"
        source_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)

        requested = requested_media(args.media)
        failure_context = {
            "schema_version": 2,
            "platform": "youtube",
            "identity": {"kind": "platform_id", "value": video_id},
            "title": title,
            "source_url": f"https://www.youtube.com/watch?v={video_id}",
            "requested_media": requested,
            "selected_audio_language": audio_format.get("language"),
        }
        video_path = source_dir / "source.mp4"
        audio_path = source_dir / f"audio.{audio_format['language']}.m4a"
        need_video = "video" in requested and (args.force or not nonempty(video_path))
        need_audio = "audio" in requested and (args.force or not nonempty(audio_path))

        audio_source: Path | None = audio_path if nonempty(audio_path) and not args.force else None
        if need_video or need_audio:
            audio_source = download_format(args, str(audio_format["format_id"]), work_dir / "audio.%(ext)s")
        if need_audio and audio_source is not None:
            make_audio(audio_source, audio_path)
        if need_video:
            if audio_source is None:
                raise RuntimeError("缺少选定的中文音轨，无法合并视频")
            video_source = download_format(
                args,
                f"bestvideo[height<={args.max_height}]/bestvideo",
                work_dir / "video.%(ext)s",
            )
            make_video(video_source, audio_source, video_path)

        missing = [
            kind
            for kind, path in (("video", video_path), ("audio", audio_path))
            if kind in requested and not nonempty(path)
        ]
        if missing:
            raise RuntimeError("缺少请求的输出：" + ", ".join(missing))

        captured_at = datetime.now().astimezone().isoformat()
        metadata = {
            **inspection,
            "source_url": f"https://www.youtube.com/watch?v={video_id}",
            "uploader": info.get("uploader"),
            "duration": info.get("duration"),
            "downloaded_at": captured_at,
            "source": "download_youtube.py",
        }
        write_json(source_dir / "metadata.json", metadata)
        artifacts: dict[str, str] = {"metadata": "source/metadata.json"}
        if nonempty(video_path):
            artifacts["video"] = "source/source.mp4"
        if nonempty(audio_path):
            artifacts["audio"] = f"source/{audio_path.name}"
        old_manifest = load_manifest(package)
        old_artifacts = old_manifest.get("artifacts") if isinstance(old_manifest.get("artifacts"), dict) else {}
        old_gate = old_manifest.get("verified_gate")
        later_gates = {"transcript_ready", "evidence_ready", "notes_complete", "summary_complete"}
        verified_gate = old_gate if old_gate in later_gates and not need_video else "media_ready"
        manifest = {
            **old_manifest,
            "schema_version": 2,
            "platform": "youtube",
            "identity": {"kind": "platform_id", "value": video_id},
            "title": title,
            "source_url": f"https://www.youtube.com/watch?v={video_id}",
            "requested_media": requested,
            "selected_audio_language": audio_format.get("language"),
            "verified_gate": verified_gate,
            "result": "complete",
            "updated_at": captured_at,
            "artifacts": {**old_artifacts, **artifacts},
        }
        manifest.pop("errors", None)
        write_json(package / "manifest.json", manifest)
        shutil.rmtree(work_dir, ignore_errors=True)
        print(json.dumps({**inspection, "requested_media": requested, "package": str(package), "artifacts": artifacts}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        error = sanitized_error(exc)
        if package is not None:
            failure = {
                **load_manifest(package),
                **failure_context,
                "schema_version": 2,
                "platform": "youtube",
                "result": "partial",
                "updated_at": datetime.now().astimezone().isoformat(),
                "errors": [error],
            }
            write_json(package / "manifest.json", failure)
        print(f"YouTube 下载失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
