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
        self.assertEqual(result["api_version"], 1)
        self.assertEqual(set(result), {"api_version", "workspace_id", "operation_id", "status", "observed_revision", "result", "artifact_refs", "validation", "provenance", "next_action", "diagnostics"})
        details = result["result"]
        self.assertEqual(details["source"]["contract_version"], 3)
        self.assertRegex(details["source"]["revision"], r"^[0-9a-f]{40}$")
        self.assertRegex(details["source"]["content_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            [item["id"] for item in details["entries"]],
            ["agent.mandarin-netease", "agent.source-notes", "skill.extract-media", "skill.mandarin-audio", "skill.practice", "skill.source-notes", "skill.review"],
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
        details = result["result"]
        receipt = self.codex_root / "video-extract/install-receipt.json"
        self.assertTrue(receipt.is_file())
        recorded = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(recorded["source"], details["source"])
        source = Path(details["source"]["path"])
        self.assertEqual(installed.resolve(), (source / "integrations/skills/source-notes").resolve())
        backup = Path(details["backup"])
        self.assertEqual((backup / "agents/skills/source-notes/SKILL.md").read_text(encoding="utf-8"), "manual edit\n")

        check_code, checked = self.run_cli("check")
        self.assertEqual(check_code, 0)
        self.assertTrue(checked["result"]["ok"])
        self.assertTrue(all(item["state"] == "linked" for item in checked["result"]["entries"]))
        self.assertEqual(checked["result"]["tool"]["source"], str(source))

    def test_apply_operation_identity_distinguishes_install_targets(self):
        first = apply(self.root / "agents-one", self.root / "codex-one")
        second = apply(self.root / "agents-two", self.root / "codex-two")

        self.assertNotEqual(first["operation_id"], second["operation_id"])

    def test_check_reports_copy_drift_and_the_exact_maintenance_path(self):
        copied = self.agents_root / "skills/extract-media"
        copied.mkdir(parents=True)
        (copied / "SKILL.md").write_text("stale copy\n", encoding="utf-8")

        code, result = self.run_cli("check")

        self.assertEqual(code, 1)
        self.assertFalse(result["result"]["ok"])
        item = next(entry for entry in result["result"]["entries"] if entry["id"] == "skill.extract-media")
        self.assertEqual(item["state"], "drifted")
        self.assertEqual(result["status"], "recoverable_failure")
        self.assertIn("source_or_install_drift", result["diagnostics"][0])
        self.assertEqual(item["maintenance_path"], str(Path(result["result"]["source"]["path"]) / "integrations/skills/extract-media"))

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

        self.assertFalse(result["result"]["ok"])
        self.assertEqual(result["status"], "recoverable_failure")
        self.assertIn("injected link failure", result["diagnostics"][0])
        self.assertFalse(first.is_symlink())
        self.assertEqual(first.read_text(encoding="utf-8"), "manual agent\n")
        self.assertFalse((self.codex_root / "agents/source-notes-operator.toml").exists())

    def test_apply_rolls_back_links_when_receipt_commit_fails(self):
        first = self.codex_root / "agents/mandarin-netease-operator.toml"
        first.parent.mkdir(parents=True)
        first.write_text("manual agent\n", encoding="utf-8")

        with patch("video_extract.manifest.atomic_write_json", side_effect=OSError("receipt fsync failed")):
            result = apply(self.agents_root, self.codex_root)

        self.assertEqual(result["status"], "recoverable_failure")
        self.assertIn("receipt fsync failed", result["diagnostics"][0])
        self.assertFalse(first.is_symlink())
        self.assertEqual(first.read_text(encoding="utf-8"), "manual agent\n")
        self.assertFalse((self.codex_root / "agents/source-notes-operator.toml").exists())
        self.assertFalse((self.codex_root / "video-extract/install-receipt.json").exists())

    def test_check_detects_engineering_revision_and_cli_drift(self):
        code, applied = self.run_cli("apply")
        self.assertEqual(code, 0)
        receipt = self.codex_root / "video-extract/install-receipt.json"
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["source"]["revision"] = "0" * 40
        receipt.write_text(json.dumps(data), encoding="utf-8")

        code, result = self.run_cli("check")

        self.assertEqual(code, 1)
        self.assertEqual(result["result"]["receipt"]["state"], "revision_drift")
        self.assertNotEqual(data["source"]["content_fingerprint"], "")
        self.assertIn("video_extract/cli.py", applied["result"]["source"]["fingerprinted_paths"])

    def test_check_reports_contract_drift_from_the_apply_receipt(self):
        code, _ = self.run_cli("apply")
        self.assertEqual(code, 0)
        receipt = self.codex_root / "video-extract/install-receipt.json"
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["source"]["contract_version"] = 999
        receipt.write_text(json.dumps(data), encoding="utf-8")

        code, result = self.run_cli("check")

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "recoverable_failure")
        self.assertEqual(result["result"]["receipt"]["state"], "contract_drift")
        self.assertIn("install plan", result["next_action"]["command"])

    def test_check_requires_a_receipt_to_verify_installed_contract(self):
        code, _ = self.run_cli("apply")
        self.assertEqual(code, 0)
        (self.codex_root / "video-extract/install-receipt.json").unlink()

        code, result = self.run_cli("check")

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "recoverable_failure")
        self.assertEqual(result["result"]["receipt"]["state"], "missing")


if __name__ == "__main__":
    unittest.main()
