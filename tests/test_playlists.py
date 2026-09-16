import json
import tempfile
import unittest
from pathlib import Path

from video_extract.playlists import build_playback_views, build_xiaoe_playback_views, verify_playback


class PlaybackViewTests(unittest.TestCase):
    def test_builds_ordered_video_only_links_and_playlist(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "source"; library = root / "library"; output = root / "playback"
            source.mkdir(); library.mkdir()
            lessons = []
            for index, title in ((2, "1.2 第二课"), (1, "1.1 第一课")):
                folder = source / title; folder.mkdir(); (folder / f"{title}.mp4").write_bytes(b"video")
                (folder / f"{title}.wav").write_bytes(b"audio")
                lessons.append({"index": index, "sort_value": index, "title": title, "video_id": f"v{index}", "is_video": True, "section": "第一章"})
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({"course_title": "课程", "lessons": lessons}, ensure_ascii=False), encoding="utf-8")
            result = build_xiaoe_playback_views(catalog, source, library, output, ["第一章"])
            playback = output / "xiaoe/课程/第一章/播放目录"
            links = sorted(playback.iterdir())
            self.assertTrue(result["ok"]); self.assertEqual([x.name for x in links], ["001 1.1 第一课.mp4", "002 1.2 第二课.mp4"])
            self.assertTrue(all(x.is_symlink() for x in links)); self.assertFalse(any(x.suffix == ".wav" for x in links))
            text = (playback.parent / "章节播放列表.m3u8").read_text(encoding="utf-8")
            self.assertLess(text.index("第一课"), text.index("第二课"))

    def test_prefers_canonical_media_package(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "source"; source.mkdir(); library = root / "library"; video = library / "items/xiaoe/v1/source/source.mp4"
            video.parent.mkdir(parents=True); video.write_bytes(b"video")
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({"course_title": "课程", "lessons": [{"index": 1, "title": "一课", "video_id": "v1", "is_video": True, "section": "第一章"}]}, ensure_ascii=False), encoding="utf-8")
            result = build_xiaoe_playback_views(catalog, source, library, root / "out", ["第一章"])
            link = next(Path(result["sections"][0]["playback_directory"]).iterdir())
            self.assertEqual(link.resolve(), video.resolve())

    def test_refuses_to_replace_real_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "source"; folder = source / "一课"; folder.mkdir(parents=True); (folder / "一课.mp4").write_bytes(b"video")
            catalog = root / "catalog.json"; catalog.write_text(json.dumps({"course_title": "课程", "lessons": [{"index": 1, "title": "一课", "video_id": "v1", "is_video": True, "section": "第一章"}]}, ensure_ascii=False), encoding="utf-8")
            playback = root / "out/xiaoe/课程/第一章/播放目录"; playback.mkdir(parents=True); (playback / "keep.txt").write_text("keep")
            with self.assertRaises(RuntimeError): build_xiaoe_playback_views(catalog, source, root / "library", root / "out", ["第一章"])
            self.assertTrue((playback / "keep.txt").exists())

    def test_builds_from_collection_catalog_without_legacy_source(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); library = root / "library"; video = library / "items/bilibili/BV1_p1/source/source.mp4"
            video.parent.mkdir(parents=True); video.write_bytes(b"video")
            catalog = library / "collections/bilibili/BV1/catalog.json"; catalog.parent.mkdir(parents=True)
            catalog.write_text(json.dumps({"platform": "bilibili", "title": "课程", "items": [{"index": 1, "id": "BV1_p1", "title": "第一课", "section": "第一章"}]}, ensure_ascii=False), encoding="utf-8")
            result = build_playback_views(catalog, library, library / "playback")
            self.assertTrue(result["ok"])
            self.assertTrue(next((library / "playback/bilibili/课程/第一章/播放目录").iterdir()).is_symlink())

    def test_verify_detects_broken_and_non_mp4_entries(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); playback = root / "playback/xiaoe/课程/第一章/播放目录"; playback.mkdir(parents=True)
            (playback / "001 第一课.mp4").symlink_to(root / "missing.mp4")
            (playback / "bad.wav").symlink_to(root / "missing.wav")
            result = verify_playback(root / "playback")
            self.assertFalse(result["ok"]); self.assertEqual(result["broken_symlinks"], 2)
            self.assertEqual(result["non_mp4_entries"], 1)


if __name__ == "__main__":
    unittest.main()
