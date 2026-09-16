import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_extract.cli import main, parser
from video_extract.manifest import atomic_write_json
from video_extract.media import MediaInventory, MediaStream


class CliContractTests(unittest.TestCase):
    def test_v5_media_plan_cli_contract(self):
        inventory = MediaInventory("youtube", "fixture", "Fixture", 3, (MediaStream("en", "audio", "en", url="audio"),), (MediaStream("v", "video", height=720),))
        output = io.StringIO()
        argv = ["video-extract", "plan", "https://youtube.com/watch?v=fixture", "--media", "audio", "--language", "zh-CN", "--json"]
        with patch("video_extract.media_workflow.resolve_inventory", return_value=inventory), patch("sys.argv", argv), contextlib.redirect_stdout(output):
            self.assertEqual(main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["schema_version"], 5)
        self.assertEqual(result["mandarin_audio"]["mode"], "external_pyvideotrans")
        self.assertNotIn("localize_audio", [x["name"] for x in result["planned_stages"]])

    def test_help_exposes_goal_commands(self):
        help_text = parser().format_help()
        self.assertIn("plan", help_text); self.assertIn("ensure", help_text)
        self.assertNotIn("localize-audio", help_text)

    def test_offline_plan_contract(self):
        inventory = MediaInventory("youtube", "fixture", "Fixture", 3, (MediaStream("zh", "audio", "zh-CN"),))
        output = io.StringIO()
        argv = ["video-extract", "plan", "https://youtube.com/watch?v=fixture", "--goal", "podcast_zh", "--json"]
        with patch("video_extract.orchestrator.resolve_inventory", return_value=inventory), patch("sys.argv", argv), contextlib.redirect_stdout(output):
            self.assertEqual(main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["requested_goals"], ["podcast_zh"]); self.assertIn("screenshots", result["exclusions"])

    def test_status_reports_goal_state_without_legacy_video_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw); audio = package / "audio/podcast.zh-CN.m4a"; audio.parent.mkdir()
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=0.1", "-c:a", "aac", str(audio)], check=True)
            atomic_write_json(package / "manifest.json", {"schema_version": 4, "platform": "youtube", "identity": "x", "request": {"goals": ["podcast_zh"]}, "artifacts": {"localized_audio": "audio/podcast.zh-CN.m4a"}, "provenance": {"localized_audio": {"kind": "native_chinese_track", "language": "zh-CN"}}})
            output = io.StringIO()
            with patch("sys.argv", ["video-extract", "status", str(package), "--json"]), contextlib.redirect_stdout(output):
                self.assertEqual(main(), 0)
            result = json.loads(output.getvalue())
            self.assertTrue(result["goal_status"]["ok"])
            self.assertNotIn("legacy", result)


if __name__ == "__main__":
    unittest.main()
