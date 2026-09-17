import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HELPER = Path("/Users/syz/.agents/skills/mandarin-audio/scripts/run.py")


class MandarinAudioHelperTests(unittest.TestCase):
    def test_native_chinese_is_normalized_without_pyvideotrans(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); package = root / "media/items/local/native"; audio = package / "media/audio.source.m4a"
            for path in (root / "project", root / "vault", audio.parent): path.mkdir(parents=True, exist_ok=True)
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=0.2", "-c:a", "aac", str(audio)], check=True)
            (package / "manifest.json").write_text(json.dumps({"schema_version": 5, "platform": "local", "identity": "native", "title": "Native",
                "request": {"type": "media", "kinds": ["audio"], "language": "zh-CN"}, "artifacts": {"source_audio": "media/audio.source.m4a"},
                "provenance": {"source_audio": {"kind": "native_chinese_track", "language": "zh-CN"}}}), encoding="utf-8")
            workspace = root / "workspace.toml"
            workspace.write_text('''schema_version = 1
[paths]
project = "project"
media = "media"
obsidian = "vault"
[obsidian]
generated = "generated"
threads = "threads"
concepts = "concepts"
review = "REVIEW.md"
''', encoding="utf-8")
            completed = subprocess.run([sys.executable, str(HELPER), str(package), "--workspace", str(workspace), "--json"], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            result = json.loads(completed.stdout)
            self.assertEqual(result["status"], "complete")
            self.assertTrue(result["spec"]["valid"])
            self.assertTrue((package / "listening/zh-CN/production-report.json").is_file())


if __name__ == "__main__": unittest.main()
