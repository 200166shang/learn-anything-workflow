import tempfile
import unittest
from pathlib import Path

from video_extract.media import MediaInventory, MediaStream, SubtitleTrack, select_audio_stream
from video_extract.media_request import MediaKind, normalize_media_request
from video_extract.package_paths import canonical_package
from video_extract.planner import build_media_plan


class MediaRequestV5Tests(unittest.TestCase):
    def setUp(self):
        self.inventory = MediaInventory(
            "youtube", "abc123", "A title", 10,
            (MediaStream("a", "audio", "en", url="audio"),),
            (MediaStream("v", "video", height=720, url="video"),),
            (SubtitleTrack("en", "official", False, url="subtitle"),),
            original_language="en",
        )

    def test_default_all_is_standard_package_without_learning_or_tts(self):
        request = normalize_media_request()
        self.assertEqual(request.kinds, (MediaKind.VIDEO, MediaKind.AUDIO, MediaKind.SUBTITLES))
        names = [x["name"] for x in build_media_plan(self.inventory, request)["planned_stages"]]
        self.assertEqual(names, ["resolve_source", "materialize_source_video", "materialize_source_audio", "materialize_best_subtitle"])
        self.assertFalse(any(x in " ".join(names) for x in ("notes", "tts", "candidate")))

    def test_each_single_media_kind_plans_no_extra_artifacts(self):
        expected = {
            "video": {"resolve_source", "materialize_source_video"},
            "audio": {"resolve_source", "materialize_source_audio"},
            "subtitles": {"resolve_source", "materialize_best_subtitle"},
        }
        for output, stages in expected.items():
            with self.subTest(output=output):
                actual = {x["name"] for x in build_media_plan(self.inventory, normalize_media_request(output))["planned_stages"]}
                self.assertEqual(actual, stages)

    def test_chinese_audio_only_uses_an_existing_native_track(self):
        request = normalize_media_request("audio", "zh-CN")
        foreign = [x["name"] for x in build_media_plan(self.inventory, request)["planned_stages"]]
        self.assertEqual(foreign, ["resolve_source"])
        plan = build_media_plan(self.inventory, request)
        self.assertEqual(plan["audio"]["reason"], "missing_requested_language_track")
        native_inventory = MediaInventory("youtube", "x", "x", 1, (MediaStream("zh", "audio", "zh-Hans", url="z"),))
        native = [x["name"] for x in build_media_plan(native_inventory, request)["planned_stages"]]
        self.assertEqual(native, ["resolve_source", "materialize_requested_audio"])

    def test_non_english_chinese_audio_is_unsupported(self):
        inventory = MediaInventory("youtube", "pt", "pt", 1, (MediaStream("pt", "audio", "pt-BR", url="a"),), original_language="pt-BR")
        plan = build_media_plan(inventory, normalize_media_request("audio", "zh-CN"))
        self.assertEqual([x["name"] for x in plan["planned_stages"]], ["resolve_source"])
        self.assertEqual(plan["audio"]["reason"], "missing_requested_language_track")

    def test_both_workflows_resolve_same_canonical_item(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.assertEqual(canonical_package(self.inventory, root), canonical_package(self.inventory, root))
            self.assertEqual(canonical_package(self.inventory, root), root / "items/youtube/abc123")

    def test_local_identity_is_content_based_not_path_based(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); first = root / "a.mp4"; second = root / "renamed.mp4"
            first.write_bytes(b"same media"); second.write_bytes(b"same media")
            a = MediaInventory("local", str(first), "a", 1, source=str(first))
            b = MediaInventory("local", str(second), "b", 1, source=str(second))
            self.assertEqual(canonical_package(a, root), canonical_package(b, root))

    def test_local_audio_stream_does_not_need_a_remote_url(self):
        inventory = MediaInventory(
            "local", "local", "local", 1,
            (MediaStream("0", "audio", codec="aac"),),
        )
        self.assertEqual(select_audio_stream(inventory, "high").id, "0")


if __name__ == "__main__": unittest.main()
