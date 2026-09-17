import tempfile
import unittest
from pathlib import Path

from video_extract.workspace import WorkspaceConfig
from video_extract.xiaoe_download import (
    course_id,
    inherit_signed_query,
    lesson_chapter,
    parse_chapters,
    paths,
    select_lessons,
    xor_key,
)


COURSE_URL = "https://shop.h5.xet.pomoho.com/p/course/ecourse/course_ABC123?sub_course_list_mode=0"


class XiaoeDownloadTests(unittest.TestCase):
    def test_parse_chapters_accepts_lists_and_ranges(self):
        self.assertEqual(parse_chapters("17,18,19"), {17, 18, 19})
        self.assertEqual(parse_chapters("17-19"), {17, 18, 19})

    def test_select_lessons_uses_title_or_section_chapter(self):
        catalog = {"lessons": [
            {"title": "17.1-导航", "section": "其他", "is_video": True},
            {"title": "Agent 入门", "section": "第十八章-在线 Agent", "is_video": True},
            {"title": "19.1-离线 Agent", "section": "第十九章", "is_video": False},
            {"title": "20.1-ROS 源码", "section": "第二十章", "is_video": True},
        ]}
        selected = select_lessons(catalog, {17, 18, 19})
        self.assertEqual([item["title"] for item in selected], ["17.1-导航", "Agent 入门"])
        self.assertEqual(lesson_chapter(catalog["lessons"][1]), 18)

    def test_workspace_paths_are_stable_and_under_media(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = WorkspaceConfig(
                root / "workspace.toml", "test", root, root / "project", root / "media", root / "vault",
                root / "vault/generated", root / "vault/threads", root / "vault/concepts", root / "vault/REVIEW.md",
            )
            resolved = paths(config, COURSE_URL)
            self.assertEqual(course_id(COURSE_URL), "course_ABC123")
            self.assertTrue(resolved["session"].is_relative_to(config.media))
            self.assertEqual(resolved["items"], config.media / "items/xiaoe")

    def test_signed_segment_url_inherits_playlist_query_without_reencoding(self):
        playlist = "https://cdn.example/video/index.m3u8?sign=a,b%2Fc&expires=123"
        self.assertEqual(
            inherit_signed_query("seg/001.ts", playlist),
            "https://cdn.example/video/seg/001.ts?sign=a,b%2Fc&expires=123",
        )
        self.assertEqual(
            inherit_signed_query("seg/002.ts?token=own", playlist),
            "https://cdn.example/video/seg/002.ts?token=own&sign=a,b%2Fc&expires=123",
        )

    def test_xor_key_uses_repeating_user_id_bytes(self):
        self.assertEqual(xor_key(bytes([1, 2, 3, 4]), "ab"), bytes([96, 96, 98, 102]))


if __name__ == "__main__":
    unittest.main()
