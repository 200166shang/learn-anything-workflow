"""Normalized media inventory and shared local/yt-dlp materialization seam."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .manifest import sanitize


@dataclass(frozen=True)
class MediaStream:
    id: str
    kind: str
    language: str | None = None
    codec: str | None = None
    height: int | None = None
    bitrate: float | None = None
    url: str | None = field(default=None, repr=False)
    muxed: bool = False
    language_preference: float | None = None
    authorization_headers: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    referer: str | None = field(default=None, repr=False, compare=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "language": self.language,
            "codec": self.codec, "height": self.height, "bitrate": self.bitrate,
            "muxed": self.muxed, "language_preference": self.language_preference,
        }


@dataclass(frozen=True)
class SubtitleTrack:
    language: str
    id: str
    automatic: bool = False
    url: str | None = field(default=None, repr=False)
    authorization_headers: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    referer: str | None = field(default=None, repr=False, compare=False)

    def public(self) -> dict[str, Any]:
        return {"language": self.language, "id": self.id, "automatic": self.automatic}


@dataclass(frozen=True)
class MediaInventory:
    platform: str
    identity: str
    title: str
    duration: float | None
    audio_streams: tuple[MediaStream, ...] = ()
    video_streams: tuple[MediaStream, ...] = ()
    subtitles: tuple[SubtitleTrack, ...] = ()
    authorization_context: str = "public_or_existing_session"
    source: str | None = field(default=None, repr=False)
    original_language: str | None = None
    default_audio_language: str | None = None

    def public(self) -> dict[str, Any]:
        return sanitize({
            "platform": self.platform, "identity": self.identity, "title": self.title,
            "duration": self.duration,
            "original_language": self.original_language,
            "default_audio_language": self.default_audio_language,
            "audio_streams": [x.public() for x in self.audio_streams],
            "video_streams": [x.public() for x in self.video_streams],
            "subtitles": [x.public() for x in self.subtitles],
            "authorization_context": self.authorization_context,
        })


def platform_for(source: str) -> str:
    path = Path(source).expanduser()
    if path.exists():
        return "local"
    host = urlparse(source).netloc.casefold()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "bilibili.com" in host or "b23.tv" in host:
        return "bilibili"
    if "xet" in host or "xiaoe" in host:
        return "xiaoe"
    raise ValueError("unsupported source; expected authorized YouTube, Bilibili, Xiaoe, or local media")


def _stream(item: dict[str, Any], kind: str, base_headers: dict[str, str] | None = None) -> MediaStream:
    headers = {str(k): str(v) for k, v in {**(base_headers or {}), **(item.get("http_headers") or {})}.items()}
    referer = item.get("referer") or next((v for k, v in headers.items() if k.casefold() == "referer"), None)
    return MediaStream(
        id=str(item.get("format_id") or item.get("id") or "unknown"), kind=kind,
        language=item.get("language"), codec=item.get("acodec") if kind == "audio" else item.get("vcodec"),
        height=item.get("height"), bitrate=item.get("abr") or item.get("tbr"), url=item.get("url"),
        muxed=item.get("vcodec") not in {None, "none"} and item.get("acodec") not in {None, "none"},
        language_preference=item.get("language_preference"), authorization_headers=headers, referer=referer,
    )


def inventory_from_ydl(info: dict[str, Any], platform: str, source: str | None = None) -> MediaInventory:
    formats = [x for x in info.get("formats", []) if isinstance(x, dict)]
    base_headers = info.get("http_headers") if isinstance(info.get("http_headers"), dict) else {}
    audio = tuple(_stream(x, "audio", base_headers) for x in formats if x.get("acodec") not in {None, "none"})
    video = tuple(_stream(x, "video", base_headers) for x in formats if x.get("vcodec") not in {None, "none"})
    subtitles = tuple(
        SubtitleTrack(lang, str(track.get("name") or lang), False, track.get("url"),
                      {str(k): str(v) for k, v in {**base_headers, **(track.get("http_headers") or {})}.items()},
                      track.get("referer"))
        for lang, tracks in (info.get("subtitles") or {}).items() for track in (tracks[-1:] if tracks else [])
    )
    return MediaInventory(platform, str(info.get("id") or source or "unknown"), str(info.get("title") or info.get("id") or "untitled"),
        float(info["duration"]) if info.get("duration") else None, audio, video, subtitles, source=source,
        original_language=info.get("original_language") or info.get("language"), default_audio_language=info.get("language"))


def resolve_inventory(source: str, allow_browser: bool = False) -> MediaInventory:
    platform = platform_for(source)
    if platform == "local":
        path = Path(source).expanduser().resolve()
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,height", "-of", "json", str(path)], capture_output=True, text=True)
        if probe.returncode:
            raise RuntimeError("local media is not ffprobe-readable")
        data = json.loads(probe.stdout); streams = data.get("streams", [])
        audio = tuple(MediaStream(str(i), "audio", codec=x.get("codec_name")) for i, x in enumerate(streams) if x.get("codec_type") == "audio")
        video = tuple(MediaStream(str(i), "video", codec=x.get("codec_name"), height=x.get("height"), muxed=bool(audio)) for i, x in enumerate(streams) if x.get("codec_type") == "video")
        return MediaInventory("local", str(path), path.stem, float(data.get("format", {}).get("duration") or 0), audio, video, source=str(path))
    if platform in {"youtube", "bilibili", "xiaoe"}:
        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError("yt-dlp is required to resolve remote media") from exc
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}) as ydl:
                info = ydl.extract_info(source, download=False)
            if not isinstance(info, dict):
                raise RuntimeError("media metadata response is invalid")
            return inventory_from_ydl(info, platform, source)
        except Exception:
            if platform == "youtube":
                raise
            if allow_browser:
                return _browser_inventory(source, platform)
            return MediaInventory(platform, source, source, None, authorization_context="persistent_browser_required", source=source)
    raise AssertionError(f"unhandled platform: {platform}")


def _browser_inventory(source: str, platform: str) -> MediaInventory:
    """Resolve authorized muxed media through the existing persistent-browser seam."""
    from convert_voice_to_article import capture_media

    session = Path("work") / ("bilibili_browser_session" if platform == "bilibili" else "browser_session")
    captured = capture_media(source, session, 30, False)
    if not captured:
        raise RuntimeError(f"{platform} media requires an authorized browser session; log in and start playback")
    best = captured[0]
    headers = getattr(best, "headers", {}) or {}
    stream = MediaStream("browser-captured", "video", codec="unknown", url=best.url, muxed=True, authorization_headers=headers, referer=best.referer)
    audio = MediaStream("browser-captured", "audio", codec="unknown", url=best.url, muxed=True, authorization_headers=headers, referer=best.referer)
    return MediaInventory(platform, source, best.title or source, None, (audio,), (stream,), (), "persistent_browser_capture", source)


def resolve_inventory_for_ensure(source: str) -> MediaInventory:
    return resolve_inventory(source, allow_browser=True)


def is_native_chinese(language: str | None) -> bool:
    value = (language or "").replace("_", "-").casefold()
    return value == "zh" or value.startswith("zh-")


def _language(value: str | None) -> str:
    return (value or "").replace("_", "-").casefold()


def _best_audio(streams: list[MediaStream], audio_quality: object) -> MediaStream | None:
    if not streams:
        return None
    independent = [stream for stream in streams if not stream.muxed]
    streams = independent or streams
    ceiling = 128 if getattr(audio_quality, "value", audio_quality) == "standard" else 192
    within = [stream for stream in streams if stream.bitrate is None or stream.bitrate <= ceiling]
    if within:
        return max(within, key=lambda stream: stream.bitrate or 0)
    return min(streams, key=lambda stream: stream.bitrate or float("inf"))


def select_audio_stream(inventory: MediaInventory, audio_quality: object, language: str | None = None) -> MediaStream | None:
    """Choose original/default language first, then preference, without depending on format order."""
    streams = [
        stream
        for stream in inventory.audio_streams
        if stream.url or inventory.platform == "local"
    ]
    if language:
        wanted = _language(language)
        return _best_audio([stream for stream in streams if _language(stream.language).startswith(wanted)], audio_quality)
    original = _language(inventory.original_language)
    default = _language(inventory.default_audio_language)
    for wanted in (original, default):
        if wanted:
            exact = [stream for stream in streams if _language(stream.language) == wanted]
            if exact:
                return _best_audio(exact, audio_quality)
    for wanted in (original, default):
        if wanted:
            base = wanted.split("-", 1)[0]
            matching = [stream for stream in streams if _language(stream.language).split("-", 1)[0] == base]
            if matching:
                return _best_audio(matching, audio_quality)
    if any(stream.language_preference is not None for stream in streams):
        preference = max(stream.language_preference if stream.language_preference is not None else float("-inf") for stream in streams)
        preferred = [stream for stream in streams if stream.language_preference == preference]
        if preferred:
            return _best_audio(preferred, audio_quality)
    return _best_audio(streams, audio_quality)


class MediaMaterializer:
    def __init__(self, ffmpeg: str = "ffmpeg", audio_bitrate: str = "192k") -> None:
        self.ffmpeg = ffmpeg
        self.audio_bitrate = audio_bitrate

    def _atomic_ffmpeg(self, inputs: list[str | MediaStream], output: Path, audio_only: bool = False) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=f".{output.stem}.", suffix=output.suffix, dir=output.parent); os.close(fd)
        temporary = Path(raw)
        try:
            command = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
            for item in inputs:
                source_url, headers, referer = _input_context(item)
                if referer:
                    command += ["-referer", referer]
                user_agent = next((v for k, v in headers.items() if k.casefold() == "user-agent"), None)
                if user_agent:
                    command += ["-user_agent", user_agent]
                extra = [(k, v) for k, v in headers.items() if k.casefold() not in {"referer", "user-agent"}]
                if extra:
                    command += ["-headers", "".join(f"{_header(k)}: {_header(v)}\r\n" for k, v in extra)]
                command += ["-i", source_url]
            if audio_only: command += ["-vn", "-c:a", "aac", "-b:a", self.audio_bitrate]
            else: command += ["-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart"]
            completed = subprocess.run([*command, str(temporary)], capture_output=True, text=True)
            if completed.returncode:
                raise RuntimeError("ffmpeg media materialization failed; authorization or stream access may need refresh")
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)

    def audio_from_local(self, source: Path, output: Path) -> None:
        self._atomic_ffmpeg([str(source)], output, True)

    def audio_from_url(self, source_url: str | MediaStream, output: Path, *, format_id: str | None = None) -> None:
        """Download a remote audio stream with yt-dlp, then normalize it.

        Passing a signed YouTube media URL straight to ffmpeg can leave ffmpeg
        waiting forever when a fragment stops responding.  yt-dlp provides
        fragment retries, socket timeouts, and resumable downloads, so use it
        as the network downloader and keep ffmpeg limited to local remuxing.
        """
        url, headers, referer = _input_context(source_url)
        # Browser-captured/course media URLs are not yt-dlp pages. Keep the
        # existing ffmpeg path for those sources; yt-dlp is specifically used
        # for YouTube media streams where fragment retry matters.
        if "youtube.com" not in urlparse(url).netloc.casefold() and "youtu.be" not in urlparse(url).netloc.casefold():
            self._atomic_ffmpeg([source_url], output, True)
            return
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="video-extract-yt-dlp-") as work:
            template = str(Path(work) / "source.%(ext)s")
            options: dict[str, Any] = {
                "outtmpl": template,
                "noplaylist": True,
                # A YouTube page can expose several dubbed audio tracks.  The
                # planner has already selected the requested language, so do
                # not silently fall back to YouTube's default audio here.
                "format": format_id or "bestaudio/best",
                "quiet": True,
                "no_warnings": True,
                "continuedl": True,
                "retries": 10,
                "fragment_retries": 10,
                "file_access_retries": 3,
                "socket_timeout": 30,
                "http_chunk_size": 10 * 1024 * 1024,
                "overwrites": True,
            }
            if headers:
                options["http_headers"] = headers
            if referer:
                options.setdefault("http_headers", {})["Referer"] = referer
            try:
                import yt_dlp
                with yt_dlp.YoutubeDL(options) as ydl:
                    ydl.download([url])
            except Exception as exc:
                raise RuntimeError("yt-dlp audio download failed after retries") from exc
            downloaded = next((p for p in Path(work).glob("source.*") if p.is_file()), None)
            if downloaded is None:
                raise RuntimeError("yt-dlp completed without producing an audio file")
            self.audio_from_local(downloaded, output)

    def video_from_url(self, source_url: str | MediaStream, output: Path) -> None:
        self._atomic_ffmpeg([source_url], output)

    def subtitle_from_url(self, source_url: str | SubtitleTrack, output: Path) -> None:
        """Materialize common SRT, WebVTT, or Bilibili JSON subtitles as SRT."""
        if isinstance(source_url, SubtitleTrack):
            if not source_url.url:
                raise RuntimeError("subtitle track has no materializable URL")
            headers = dict(source_url.authorization_headers)
            if source_url.referer:
                headers.setdefault("Referer", source_url.referer)
            request = urllib.request.Request(source_url.url, headers=headers)
        else:
            request = urllib.request.Request(source_url)
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8-sig")
        output.parent.mkdir(parents=True, exist_ok=True)
        text = _subtitle_to_srt(raw)
        fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, output)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def copy_local_video(self, source: Path, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.partial")
        shutil.copy2(source, temporary); temporary.replace(output)


def _header(value: object) -> str:
    return str(value).replace("\r", "").replace("\n", "")


def _input_context(item: str | MediaStream) -> tuple[str, dict[str, str], str | None]:
    if isinstance(item, MediaStream):
        if not item.url:
            raise RuntimeError("media stream has no materializable URL")
        return item.url, dict(item.authorization_headers), item.referer
    return str(item), {}, None


def _srt_timestamp(seconds: float) -> str:
    millis = max(0, round(seconds * 1000)); hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000); secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _subtitle_to_srt(raw: str) -> str:
    stripped = raw.strip()
    if "-->" in stripped and not stripped.startswith("WEBVTT"):
        return stripped + "\n"
    if stripped.startswith("{"):
        data = json.loads(stripped); rows = data.get("body") or data.get("data", {}).get("body") or []
        blocks = []
        for index, row in enumerate(rows, 1):
            text = str(row.get("content") or row.get("text") or "").strip()
            if text:
                blocks.append(f"{index}\n{_srt_timestamp(float(row['from']))} --> {_srt_timestamp(float(row['to']))}\n{text}\n")
        if not blocks:
            raise ValueError("subtitle JSON has no timed rows")
        return "\n".join(blocks)
    blocks = []; index = 1
    for chunk in stripped.removeprefix("WEBVTT").strip().split("\n\n"):
        lines = [line.strip() for line in chunk.splitlines() if line.strip()]
        timing = next((line for line in lines if "-->" in line), None)
        if timing:
            pos = lines.index(timing); text = " ".join(lines[pos + 1:]).strip()
            if text:
                blocks.append(f"{index}\n{timing.replace('.', ',')}\n{text}\n"); index += 1
    if not blocks:
        raise ValueError("subtitle has no timed cues")
    return "\n".join(blocks)
