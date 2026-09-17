import json
import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from video_extract.goals import normalize_request
from video_extract.cli import main
from video_extract.manifest import atomic_write_json, read_json
from video_extract.media import MediaInventory, MediaStream, SubtitleTrack
from video_extract.orchestrator import EnsureDependencies, ensure, existing_capabilities, plan
from video_extract.tts import FakeSynthesizer
from video_extract.validate import validate_goals


def make_audio(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=0.2", "-c:a", "aac", str(path)], check=True)


def make_video(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=size=32x32:rate=1:duration=1", "-f", "lavfi", "-i", "sine=duration=1", "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(path)], check=True)


class FixtureMaterializer:
    def __init__(self, bitrate: str, files: dict[str, Path], subtitle: str | None = None):
        self.bitrate = bitrate; self.files = files; self.subtitle = subtitle

    def _copy(self, source: str | Path, output: Path):
        raw = source.url if isinstance(source, MediaStream) else source
        output.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(self.files.get(str(raw), Path(raw)), output)

    def audio_from_url(self, source, output): self._copy(source, output)
    def video_from_url(self, source, output): self._copy(source, output)
    def audio_from_local(self, source, output): self._copy(source, output)
    def copy_local_video(self, source, output): self._copy(source, output)
    def subtitle_from_url(self, source, output):
        output.parent.mkdir(parents=True, exist_ok=True); output.write_text(self.subtitle, encoding="utf-8")


class EnsureEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.audio = self.root / "fixtures/audio.m4a"; self.video = self.root / "fixtures/video.mp4"
        make_audio(self.audio); make_video(self.video)
        self.bitrates = []

    def tearDown(self): self.temp.cleanup()

    @property
    def transcript(self):
        return "1\n00:00:00,000 --> 00:00:00,800\nHello world\n"

    def dependencies(self, inventory, *, formal=False):
        files = {"audio": self.audio, "video": self.video, str(self.video): self.video}
        def factory(bitrate):
            self.bitrates.append(bitrate); return FixtureMaterializer(bitrate, files, self.transcript if formal else None)
        def transcriber(audio, output):
            output.parent.mkdir(parents=True, exist_ok=True); output.write_text(self.transcript, encoding="utf-8")
            return {"engine": "fixture-whisper", "language": "en"}
        def prepare(package, video, transcript, approved):
            image = package / "frames/keyframes/frame_001.jpg"; image.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (8, 8), "red").save(image)
            sheet = package / "frames/contact_sheet.jpg"; Image.new("RGB", (8, 8), "blue").save(sheet)
            atomic_write_json(package / "review/keyframes.json", {"candidates": [{"id": "frame_001", "timestamp": 0.2, "image": "frames/keyframes/frame_001.jpg"}]})
            notes_input = package / "notes/notes_input.md"; notes_input.parent.mkdir(parents=True, exist_ok=True)
            notes_input.write_text("# 输入\n00:00:00 原始转写与画面候选", encoding="utf-8")
        return EnsureDependencies(lambda source: inventory, factory, transcriber, prepare, FakeSynthesizer)

    def legacy_audio_package(self, language: str, schema_version: int = 2) -> Path:
        package = self.root / f"legacy-v{schema_version}-{language}"; legacy = package / f"source/audio.{language}.m4a"
        legacy.parent.mkdir(parents=True); shutil.copy2(self.audio, legacy)
        atomic_write_json(package / "manifest.json", {
            "schema_version": schema_version, "platform": "youtube", "identity": {"kind": "platform_id", "value": "legacy"},
            "title": "Legacy", "selected_audio_language": language,
            "artifacts": {"metadata": "source/metadata.json", "audio": f"source/audio.{language}.m4a"},
        })
        return package

    def test_legacy_chinese_audio_upgrades_without_inventory_or_download(self):
        for schema_version in (1, 2, 3):
            with self.subTest(schema_version=schema_version):
                package = self.legacy_audio_package("zh-Hans", schema_version)
                deps = self.dependencies(MediaInventory("youtube", "unused", "Unused", 1))
                deps.inventory_resolver = lambda _source: self.fail("legacy native Chinese reuse must not resolve network inventory")
                result = ensure("https://youtube.invalid/legacy", package, normalize_request(["podcast_zh"]), deps)
                self.assertEqual(result["status"], "complete")
                manifest = read_json(package / "manifest.json")
                self.assertEqual(manifest["schema_version"], 4)
                self.assertEqual(manifest["artifacts"]["source_audio"], "source/audio.zh-Hans.m4a")
                self.assertEqual(manifest["artifacts"]["localized_audio"], "audio/podcast.zh-CN.m4a")
                self.assertTrue(manifest["provenance"]["localized_audio"]["reused_legacy_audio"])
                self.assertEqual(manifest["provenance"]["localized_audio"]["source_schema_version"], schema_version)
                self.assertTrue(validate_goals(package, ["podcast_zh"])["ok"])
                self.assertFalse(any(package.rglob("*.partial*")))

    def test_legacy_non_chinese_audio_is_source_only(self):
        package = self.legacy_audio_package("en-US")
        inventory = MediaInventory("youtube", "legacy", "Legacy", 1, (MediaStream("remote", "audio", "de", url="audio"),), original_language="en-US")
        deps = self.dependencies(inventory)
        class NoDownload(FixtureMaterializer):
            def audio_from_url(self, *_args): raise AssertionError("legacy source audio must be reused")
        deps.materializer_factory = lambda bitrate: NoDownload(bitrate, {"audio": self.audio})
        result = ensure("https://youtube.invalid/legacy", package, normalize_request(["podcast_zh"]), deps)
        self.assertEqual((result["status"], result["action"]), ("awaiting_ai", "podcast_translate"))
        manifest = read_json(package / "manifest.json")
        self.assertEqual(manifest["schema_version"], 4)
        self.assertEqual(manifest["artifacts"]["source_audio"], "source/audio.en-US.m4a")
        self.assertNotIn("localized_audio", manifest["artifacts"])
        self.assertFalse(validate_goals(package, ["podcast_zh"])["ok"])

    def test_native_chinese_podcast_completes_and_quality_is_applied(self):
        inventory = MediaInventory("youtube", "native", "Native", 1, (MediaStream("zh", "audio", "zh-CN", url="audio"),))
        package = self.root / "native"
        result = ensure("https://youtube.invalid/native", package, normalize_request(["podcast_zh"], audio_quality="standard"), self.dependencies(inventory))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.bitrates, ["128k"])
        self.assertEqual(read_json(package / "manifest.json")["provenance"]["localized_audio"]["audio_bitrate"], "128k")
        self.assertFalse((package / "frames").exists())
        planned = plan("https://youtube.invalid/native", normalize_request(["podcast_zh"]), package, lambda _: inventory)
        self.assertNotIn("normalize_native_chinese_audio", [stage["name"] for stage in planned["planned_stages"]])

    def test_goal_workflow_remains_internal_after_public_cli_removal(self):
        inventory = MediaInventory("youtube", "internal-native", "Internal Native", 1, (MediaStream("zh", "audio", "zh-CN", url="audio"),))
        package = self.root / "internal-native"
        result = ensure("https://youtube.invalid/internal-native", package, normalize_request(["podcast_zh"]), self.dependencies(inventory))
        self.assertEqual(result["status"], "complete")

    def test_existing_capabilities_rejects_escaping_artifact_paths(self):
        package = self.root / "unsafe"; package.mkdir()
        atomic_write_json(package / "manifest.json", {"schema_version": 4, "platform": "local", "identity": "unsafe", "artifacts": {"source_video": "../fixtures/video.mp4"}})
        self.assertNotIn("source_video_ready", existing_capabilities(package))

    def test_formal_subtitle_is_materialized_without_transcriber(self):
        inventory = MediaInventory("youtube", "formal", "Formal", 1, (MediaStream("en", "audio", "en", url="audio"),), subtitles=(SubtitleTrack("en", "formal", url="subtitle"),))
        deps = self.dependencies(inventory, formal=True)
        deps.transcriber = lambda *_: self.fail("formal subtitle must bypass Whisper")
        result = ensure("https://youtube.invalid/formal", self.root / "formal", normalize_request(["podcast_zh"]), deps)
        self.assertEqual(result["status"], "awaiting_ai")
        self.assertEqual(result["action"], "podcast_translate")

    def test_non_chinese_podcast_pauses_then_resumes_with_fake_tts(self):
        inventory = MediaInventory("youtube", "foreign", "Foreign", 1, (MediaStream("en", "audio", "en", url="audio"),))
        package = self.root / "foreign"; deps = self.dependencies(inventory)
        first = ensure("https://youtube.invalid/foreign", package, normalize_request(["podcast_zh"]), deps)
        self.assertEqual((first["status"], first["action"]), ("awaiting_ai", "podcast_translate"))
        atomic_write_json(package / first["output"], {"segments": [{"id": "1", "start": 0, "end": 0.8, "text": "你好，世界"}]})
        second = ensure("https://youtube.invalid/foreign", package, normalize_request(["podcast_zh"]), deps)
        self.assertEqual(second["status"], "complete")
        self.assertTrue((package / "audio/podcast.zh-CN.m4a").exists())

    def test_source_audio_prefers_original_language_over_first_dub(self):
        inventory = MediaInventory("youtube", "ddq", "Foreign", 1, (
            MediaStream("de-first", "audio", "de", bitrate=160, url="audio"),
            MediaStream("en-low", "audio", "en-US", bitrate=96, url="audio"),
            MediaStream("en-high", "audio", "en-US", bitrate=192, url="audio"),
        ), original_language="en-US", default_audio_language="en-US")
        package = self.root / "ddq-order"; deps = self.dependencies(inventory)
        result = ensure("https://youtube.invalid/ddq", package, normalize_request(["podcast_zh"]), deps)
        self.assertEqual(result["action"], "podcast_translate")
        self.assertEqual(read_json(package / "manifest.json")["stages"]["source_audio_ready"]["parameters"]["stream"], "en-high")

    def test_native_chinese_chooses_highest_bitrate_for_quality(self):
        inventory = MediaInventory("youtube", "zh", "Chinese", 1, (
            MediaStream("zh-low", "audio", "zh-CN", bitrate=64, url="audio"),
            MediaStream("zh-high", "audio", "zh-CN", bitrate=192, url="audio"),
        ))
        package = self.root / "zh-quality"; deps = self.dependencies(inventory)
        ensure("https://youtube.invalid/zh", package, normalize_request(["podcast_zh"]), deps)
        self.assertEqual(read_json(package / "manifest.json")["provenance"]["localized_audio"]["stream_id"], "zh-high")

    def _complete_notes(self, package: Path, inventory: MediaInventory, keep: str, preexisting=False, source="https://youtube.invalid/notes"):
        if preexisting:
            package.mkdir(parents=True); shutil.copy2(self.video, package / "existing.mp4")
            atomic_write_json(package / "manifest.json", {"schema_version": 4, "platform": "youtube", "identity": "notes", "artifacts": {"source_video": "existing.mp4"}})
        deps = self.dependencies(inventory)
        first = ensure(source, package, normalize_request(["notes_zh"], keep), deps)
        self.assertEqual((first["status"], first["action"]), ("awaiting_ai", "evidence_select"))
        atomic_write_json(package / first["output"], {"review_mode": "model_only", "approved_by": "codex", "approved": [{"id": "frame_001", "timestamp": 0.2, "image": "frames/keyframes/frame_001.jpg"}]})
        second = ensure(source, package, normalize_request(["notes_zh"], keep), deps)
        self.assertEqual((second["status"], second["action"]), ("awaiting_ai", "notes_write"))
        notes = package / second["output"]; notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text("# 核心知识点\n结论 [00:00:00](../source/transcript.srt)\n![证据](../frames/keyframes/frame_001.jpg)\n", encoding="utf-8")
        third = ensure(source, package, normalize_request(["notes_zh"], keep), deps)
        self.assertEqual(third["status"], "complete")
        if inventory.platform != "local" and not preexisting and keep != "yes":
            self.assertFalse((package / "source/source.mp4").exists(), read_json(package / "manifest.json"))
        fourth = ensure(source, package, normalize_request(["notes_zh"], keep), deps)
        self.assertEqual(fourth["status"], "complete")
        return read_json(package / "manifest.json")

    def test_notes_flow_reaches_both_ai_pauses_and_cleans_ephemeral_proxy(self):
        inventory = MediaInventory("youtube", "notes", "Notes", 1, (MediaStream("a", "audio", "en", url="audio"),), (MediaStream("v", "video", height=480, url="video", muxed=True),))
        package = self.root / "notes-ephemeral"
        manifest = self._complete_notes(package, inventory, "no")
        self.assertFalse((package / "source/source.mp4").exists())
        self.assertTrue(manifest["retention"]["proxy_removed_after_verification"])

    def test_existing_invalid_notes_return_to_notes_write_pause(self):
        inventory = MediaInventory("youtube", "notes", "Notes", 1, (MediaStream("a", "audio", "en", url="audio"),), (MediaStream("v", "video", height=480, url="video", muxed=True),))
        package = self.root / "invalid-existing-notes"; deps = self.dependencies(inventory)
        first = ensure("https://youtube.invalid/notes", package, normalize_request(["notes_zh"], "yes"), deps)
        atomic_write_json(package / first["output"], {"review_mode": "model_only", "approved_by": "codex", "approved": [{"id": "frame_001", "timestamp": 0.2, "image": "frames/keyframes/frame_001.jpg"}]})
        notes = package / "notes/notes.md"; notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text("# 旧笔记\n没有必需章节、时间戳或批准图片。\n", encoding="utf-8")
        second = ensure("https://youtube.invalid/notes", package, normalize_request(["notes_zh"], "yes"), deps)
        self.assertEqual((second["status"], second["action"]), ("awaiting_ai", "notes_write"))
        self.assertEqual(second["output"], "notes/notes.md")

    def test_notes_uses_existing_deterministic_candidate_preparation(self):
        inventory = MediaInventory("youtube", "prepare", "Prepare", 1, (MediaStream("a", "audio", "en", url="audio"),), (MediaStream("v", "video", height=480, url="video", muxed=True),))
        deps = self.dependencies(inventory); deps.evidence_preparer = EnsureDependencies().evidence_preparer
        package = self.root / "real-prepare"
        result = ensure("https://youtube.invalid/prepare", package, normalize_request(["notes_zh"]), deps)
        self.assertEqual((result["status"], result["action"]), ("awaiting_ai", "evidence_select"))
        self.assertTrue((package / "review/keyframes.json").is_file())
        self.assertTrue((package / "notes/notes_input.md").is_file())
        self.assertFalse((package / "review/approved_keyframes.json").exists())

    def test_preexisting_remote_package_video_is_preserved(self):
        inventory = MediaInventory("youtube", "notes", "Notes", 1, (MediaStream("a", "audio", "en", url="audio"),), (MediaStream("v", "video", height=480, url="video", muxed=True),))
        package = self.root / "notes-existing"
        manifest = self._complete_notes(package, inventory, "no", preexisting=True)
        self.assertTrue((package / "existing.mp4").exists())
        self.assertTrue(manifest["retention"]["source_video_preexisted"])
        self.assertNotIn("proxy_removed_after_verification", manifest["retention"])

    def test_local_adapter_flow_preserves_local_video(self):
        inventory = MediaInventory("local", str(self.video), "Local", 1, (MediaStream("a", "audio", codec="aac"),), (MediaStream("v", "video", height=32, muxed=True),), source=str(self.video))
        package = self.root / "notes-local"
        manifest = self._complete_notes(package, inventory, "no", source=str(self.video))
        self.assertTrue(self.video.exists())
        self.assertTrue((package / "source/source.mp4").exists())
        self.assertTrue(manifest["retention"]["source_video_preexisted"])


if __name__ == "__main__": unittest.main()
