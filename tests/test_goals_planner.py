import unittest

from video_extract.goals import Goal, KeepVideo, normalize_request
from video_extract.media import MediaInventory, MediaStream, SubtitleTrack, inventory_from_ydl
from video_extract.planner import build_plan


class GoalPlannerTests(unittest.TestCase):
    def test_goal_parsing_and_defaults(self):
        request = normalize_request(["podcast_zh"])
        self.assertEqual(request.goals, (Goal.PODCAST_ZH,))
        self.assertEqual(request.resolved_video_quality, 480)
        self.assertEqual(normalize_request(["notes_zh"], "yes").resolved_video_quality, 720)

    def test_invalid_goals(self):
        for goals in ([], ["unknown"], ["notes_zh", "notes_zh"]):
            with self.subTest(goals=goals), self.assertRaises(ValueError):
                normalize_request(goals)

    def test_native_podcast_has_no_visual_or_tts_stages(self):
        inventory = MediaInventory("youtube", "x", "title", 10, (MediaStream("a", "audio", "zh-Hans", url="secret"),))
        plan = build_plan(inventory, normalize_request(["podcast_zh"]))
        names = [x["name"] for x in plan["planned_stages"]]
        self.assertIn("normalize_native_chinese_audio", names)
        self.assertNotIn("materialize_source_audio", names)
        self.assertEqual(plan["selected_audio"]["id"], "a")
        self.assertFalse(any(word in name for name in names for word in ("candidate", "evidence", "notes", "translate", "synthesize")))

    def test_formal_subtitle_bypasses_asr(self):
        inventory = MediaInventory("youtube", "x", "title", 10, (MediaStream("a", "audio", "en"),), subtitles=(SubtitleTrack("en", "s"),))
        names = [x["name"] for x in build_plan(inventory, normalize_request(["podcast_zh"]))["planned_stages"]]
        self.assertIn("materialize_formal_subtitle", names)
        self.assertNotIn("transcribe_source_audio", names)

    def test_notes_always_plans_visual_evidence(self):
        inventory = MediaInventory("local", "x", "title", 10, video_streams=(MediaStream("v", "video"),))
        names = [x["name"] for x in build_plan(inventory, normalize_request(["notes_zh"], KeepVideo.NO))["planned_stages"]]
        self.assertTrue({"generate_candidates", "select_evidence", "write_notes"}.issubset(names))

    def test_public_inventory_omits_signed_urls(self):
        inventory = inventory_from_ydl({"id": "x", "formats": [{"format_id": "1", "acodec": "aac", "vcodec": "none", "url": "https://x.invalid/media?token=secret"}]}, "youtube")
        self.assertNotIn("secret", repr(inventory.public()))


if __name__ == "__main__":
    unittest.main()
