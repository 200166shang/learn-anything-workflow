import json
import tempfile
import unittest
import wave
from pathlib import Path

from video_extract.validate import validate_media_package_state, validate_media_request


class PartialMediaValidationTests(unittest.TestCase):
    def make_package(self, root: Path) -> Path:
        package = root / "item"; audio = package / "media/audio.source.m4a"; audio.parent.mkdir(parents=True)
        with wave.open(str(audio), "wb") as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000); stream.writeframes(b"\0\0" * 8000)
        manifest = {"schema_version": 5, "platform": "youtube", "identity": "abcdefghijk", "request": {"type": "media", "kinds": ["audio"], "language": "zh-CN"}, "artifacts": {"source_audio": "media/audio.source.m4a"}, "provenance": {"source_audio": {"kind": "source_audio"}}, "pause": {"status": "awaiting_localization", "action": "localize-audio", "input": "media/audio.source.m4a", "output": "media/audio.zh-CN.m4a"}}
        (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return package

    def test_valid_pause_does_not_claim_completion(self):
        with tempfile.TemporaryDirectory() as raw:
            package = self.make_package(Path(raw)); result = validate_media_package_state(package)
            self.assertTrue(result["ok"]); self.assertFalse(result["complete"]); self.assertEqual(result["state"], "awaiting_localization")
            self.assertFalse(validate_media_request(package)["ok"])

    def test_completed_validation_is_not_weakened(self):
        with tempfile.TemporaryDirectory() as raw:
            package = self.make_package(Path(raw)); manifest = json.loads((package / "manifest.json").read_text()); manifest.pop("pause")
            (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = validate_media_package_state(package)
            self.assertFalse(result["ok"]); self.assertEqual(result["state"], "complete")

    def test_pause_rejects_claimed_but_invalid_localized_audio(self):
        with tempfile.TemporaryDirectory() as raw:
            package = self.make_package(Path(raw)); manifest = json.loads((package / "manifest.json").read_text()); manifest["artifacts"]["localized_audio"] = "media/missing.m4a"
            (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertFalse(validate_media_package_state(package)["ok"])

    def test_request_artifact_form_is_a_valid_pause(self):
        with tempfile.TemporaryDirectory() as raw:
            package = self.make_package(Path(raw)); manifest = json.loads((package / "manifest.json").read_text())
            import hashlib
            request = {"source": {"audio": "media/audio.source.m4a", "audio_sha256": hashlib.sha256((package / "media/audio.source.m4a").read_bytes()).hexdigest()}, "target": {"audio": "media/audio.zh-CN.m4a"}}
            (package / "localization").mkdir(); (package / "localization/request.json").write_text(json.dumps(request))
            manifest["artifacts"]["localization_request"] = "localization/request.json"; manifest["pause"] = {"status": "awaiting_localization", "action": "localize-audio", "request": "localization/request.json"}
            (package / "manifest.json").write_text(json.dumps(manifest))
            self.assertTrue(validate_media_package_state(package)["ok"])

    def test_synthesized_completed_audio_requires_supporting_artifacts(self):
        with tempfile.TemporaryDirectory() as raw:
            package = self.make_package(Path(raw)); manifest = json.loads((package / "manifest.json").read_text()); manifest.pop("pause")
            audio = package / "media/audio.zh-CN.m4a"; audio.write_bytes((package / "media/audio.source.m4a").read_bytes())
            (package / "localization").mkdir()
            for name in ("request.json", "script.json", "report.json"): (package / "localization" / name).write_text("{}")
            manifest["artifacts"].update({"localized_audio": "media/audio.zh-CN.m4a", "localization_request": "localization/request.json", "localized_script": "localization/script.json", "localization_report": "localization/report.json"})
            manifest["provenance"]["localized_audio"] = {"kind": "synthesized", "request_id": "request-1"}
            (package / "manifest.json").write_text(json.dumps(manifest))
            self.assertTrue(validate_media_request(package)["ok"])


if __name__ == "__main__": unittest.main()
