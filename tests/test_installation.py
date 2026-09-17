import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_extract.cli import main
from video_extract.installation import apply


class InstallationContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.agents_root = self.root / ".agents"
        self.codex_root = self.root / ".codex"

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, dict]:
        stdout = io.StringIO()
        argv = [
            "video-extract",
            "install",
            *arguments,
            "--agents-root",
            str(self.agents_root),
            "--codex-root",
            str(self.codex_root),
            "--json",
        ]
        with patch("sys.argv", argv), contextlib.redirect_stdout(stdout):
            code = main()
        return code, json.loads(stdout.getvalue())

    def test_plan_reports_the_unique_source_and_changes_nothing(self):
        code, result = self.run_cli("plan")

        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "completed")
        self.assertRegex(result["source"]["revision"], r"^[0-9a-f]{40}$")
        self.assertRegex(result["source"]["content_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            [item["id"] for item in result["entries"]],
            ["agent.mandarin-netease", "agent.source-notes", "skill.extract-media", "skill.mandarin-audio", "skill.source-notes"],
        )
        self.assertFalse(self.agents_root.exists())
        self.assertFalse(self.codex_root.exists())

    def test_apply_preserves_manual_edits_before_linking_to_source(self):
        installed = self.agents_root / "skills/source-notes"
        installed.mkdir(parents=True)
        (installed / "SKILL.md").write_text("manual edit\n", encoding="utf-8")

        code, result = self.run_cli("apply")

        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "completed")
        source = Path(result["source"]["path"])
        self.assertEqual(installed.resolve(), (source / "integrations/skills/source-notes").resolve())
        backup = Path(result["backup"])
        self.assertEqual((backup / "agents/skills/source-notes/SKILL.md").read_text(encoding="utf-8"), "manual edit\n")

        check_code, checked = self.run_cli("check")
        self.assertEqual(check_code, 0)
        self.assertTrue(checked["ok"])
        self.assertTrue(all(item["state"] == "linked" for item in checked["entries"]))
        self.assertEqual(checked["tool"]["source"], str(source))

    def test_check_reports_copy_drift_and_the_exact_maintenance_path(self):
        copied = self.agents_root / "skills/extract-media"
        copied.mkdir(parents=True)
        (copied / "SKILL.md").write_text("stale copy\n", encoding="utf-8")

        code, result = self.run_cli("check")

        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        item = next(entry for entry in result["entries"] if entry["id"] == "skill.extract-media")
        self.assertEqual(item["state"], "drifted")
        self.assertEqual(result["status"], "installation_drift")
        self.assertEqual(item["maintenance_path"], str(Path(result["source"]["path"]) / "integrations/skills/extract-media"))

    def test_apply_rolls_back_every_target_when_linking_is_interrupted(self):
        first = self.codex_root / "agents/mandarin-netease-operator.toml"
        first.parent.mkdir(parents=True)
        first.write_text("manual agent\n", encoding="utf-8")
        original = Path.symlink_to
        calls = 0

        def interrupted(path: Path, target: Path, target_is_directory: bool = False):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected link failure")
            return original(path, target, target_is_directory=target_is_directory)

        with patch.object(Path, "symlink_to", interrupted):
            result = apply(self.agents_root, self.codex_root)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "recoverable_failure")
        self.assertIn("injected link failure", result["diagnostics"][0])
        self.assertFalse(first.is_symlink())
        self.assertEqual(first.read_text(encoding="utf-8"), "manual agent\n")
        self.assertFalse((self.codex_root / "agents/source-notes-operator.toml").exists())


if __name__ == "__main__":
    unittest.main()
