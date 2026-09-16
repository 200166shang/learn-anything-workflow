import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from video_extract.manifest import atomic_write_json
from video_extract.podcast import prepare_translation_batches, validate_localized_script
from video_extract.validate import validate, validate_goals


class PodcastValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_batches_and_script_complete_coverage(self):
        transcript = self.root / "transcript.srt"
        transcript.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n\n2\n00:00:02,000 --> 00:00:04,000\nWorld\n", encoding="utf-8")
        batches = prepare_translation_batches(transcript, self.root / "batches.json", max_characters=5)
        self.assertEqual(len(batches["batches"]), 2)
        script = {"segments": [{"id": "1", "start": 0, "end": 2, "text": "你好"}, {"id": "2", "start": 2, "end": 4, "text": "世界"}]}
        self.assertEqual(validate_localized_script(script, batches), [])
        self.assertTrue(validate_localized_script({"segments": script["segments"][:1]}, batches))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
    def test_audio_only_package_validates(self):
        audio = self.root / "audio" / "podcast.zh-CN.m4a"; audio.parent.mkdir()
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.1", "-c:a", "aac", str(audio)], check=True)
        atomic_write_json(self.root / "manifest.json", {"schema_version": 4, "platform": "local", "identity": "fixture", "artifacts": {"localized_audio": "audio/podcast.zh-CN.m4a"}, "provenance": {"localized_audio": {"kind": "native_chinese_track", "language": "zh-CN"}}})
        self.assertTrue(validate_goals(self.root, ["podcast_zh"])["ok"])

    def test_v3_manifest_remains_readable(self):
        atomic_write_json(self.root / "manifest.json", {"schema_version": 3, "platform": "local", "identity": "old", "artifacts": {}})
        self.assertEqual(validate(self.root).schema_version, 3)


if __name__ == "__main__":
    unittest.main()
