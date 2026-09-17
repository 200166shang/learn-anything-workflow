import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from convert_voice_to_article import MediaCandidate
from video_extract.media import MediaInventory, MediaMaterializer, MediaStream, _subtitle_to_srt, inventory_from_ydl, resolve_inventory, resolve_inventory_for_ensure
from video_extract.goals import normalize_request
from video_extract.manifest import atomic_write_json
from video_extract.orchestrator import plan


class FailingYdl:
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def extract_info(self, *_args, **_kwargs): raise RuntimeError("direct metadata unavailable")


class MediaAdapterTests(unittest.TestCase):
    def test_youtube_materializer_preserves_selected_dubbed_audio_format(self):
        observed = {}

        class RecordingYdl:
            def __init__(self, options): observed["options"] = options
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def download(self, urls):
                observed["urls"] = urls
                Path(observed["options"]["outtmpl"].replace("%(ext)s", "m4a")).write_bytes(b"fixture")

        fake_module = types.SimpleNamespace(YoutubeDL=RecordingYdl)
        with tempfile.TemporaryDirectory() as raw, \
                patch.dict(sys.modules, {"yt_dlp": fake_module}), \
                patch.object(MediaMaterializer, "audio_from_local") as normalize:
            output = Path(raw) / "audio.zh-CN.m4a"
            MediaMaterializer().audio_from_url(
                "https://www.youtube.com/watch?v=fixture",
                output,
                format_id="140-12",
            )

        self.assertEqual(observed["options"]["format"], "140-12")
        self.assertEqual(observed["urls"], ["https://www.youtube.com/watch?v=fixture"])
        normalize.assert_called_once()

    def test_bilibili_and_xiaoe_ensure_browser_fallbacks_return_materializable_streams(self):
        fake_module = types.SimpleNamespace(YoutubeDL=lambda _options: FailingYdl())
        captured = [MediaCandidate("https://media.invalid/stream.m3u8?token=secret", "https://ref.invalid", "https://page.invalid", "Captured", 10)]
        for source, platform in (("https://www.bilibili.com/video/BV1xx", "bilibili"), ("https://course.xiaoe.com/video/1", "xiaoe")):
            with self.subTest(platform=platform), patch.dict(sys.modules, {"yt_dlp": fake_module}), patch("convert_voice_to_article.capture_media", return_value=captured):
                inventory = resolve_inventory_for_ensure(source)
                self.assertEqual(inventory.platform, platform)
                self.assertTrue(inventory.audio_streams and inventory.video_streams)
                self.assertEqual(inventory.authorization_context, "persistent_browser_capture")
                self.assertNotIn("secret", repr(inventory.public()))

    def test_plan_never_invokes_persistent_browser_fallback(self):
        fake_module = types.SimpleNamespace(YoutubeDL=lambda _options: FailingYdl())
        with patch.dict(sys.modules, {"yt_dlp": fake_module}), patch("convert_voice_to_article.capture_media") as capture:
            result = plan("https://course.xiaoe.com/video/1", normalize_request(["notes_zh"]))
        capture.assert_not_called()
        self.assertEqual(result["blockers"], ["persistent browser authorization is required"])
        self.assertEqual(result["planned_stages"][0]["name"], "resolve_source")

    def test_inventory_keeps_language_and_authorization_context_private(self):
        info = {"id": "ddq", "language": "en-US", "formats": [{"format_id": "de", "acodec": "opus", "vcodec": "none", "language": "de", "abr": 128, "url": "https://x/de?token=secret", "http_headers": {"Authorization": "Bearer secret"}, "referer": "https://private.invalid/ref"}]}
        inventory = inventory_from_ydl(info, "youtube")
        self.assertEqual(inventory.original_language, "en-US")
        self.assertEqual(inventory.default_audio_language, "en-US")
        rendered = repr(inventory) + repr(inventory.public())
        self.assertNotIn("Bearer secret", rendered)
        self.assertNotIn("private.invalid", rendered)
        self.assertNotIn("token=secret", rendered)
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "inventory.json"; atomic_write_json(output, inventory.public())
            persisted = output.read_text(encoding="utf-8")
        self.assertNotIn("Bearer secret", persisted)
        self.assertNotIn("private.invalid", persisted)
        self.assertNotIn("token=secret", persisted)

    def test_webvtt_and_bilibili_json_convert_to_srt(self):
        vtt = _subtitle_to_srt("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello")
        bili = _subtitle_to_srt('{"body":[{"from":0,"to":1,"content":"你好"}]}')
        self.assertIn("00:00:00,000 --> 00:00:01,000", vtt)
        self.assertIn("你好", bili)

    def test_materializer_passes_hidden_stream_headers_to_ffmpeg(self):
        stream = MediaStream("captured", "audio", url="https://media.invalid/a", authorization_headers={"User-Agent": "Fixture Agent", "Authorization": "Bearer secret"}, referer="https://course.invalid/watch")
        observed = []
        def fake_run(command, **_kwargs):
            observed.extend(command); Path(command[-1]).write_bytes(b"fixture")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as raw, patch("video_extract.media.subprocess.run", side_effect=fake_run):
            MediaMaterializer().audio_from_url(stream, Path(raw) / "audio.m4a")
        joined = " ".join(observed)
        self.assertIn("-referer https://course.invalid/watch", joined)
        self.assertIn("-user_agent Fixture Agent", joined)
        self.assertIn("Authorization: Bearer secret", joined)
        self.assertNotIn("Bearer secret", repr(stream.public()))


if __name__ == "__main__": unittest.main()
