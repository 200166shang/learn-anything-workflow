import tempfile
import unittest
from pathlib import Path

from video_extract.library import library_status, rebuild_library, search_library, update_package
from video_extract.manifest import atomic_write_json


def make_index_package(root: Path, identity: str = "v_robot", note_text: str | None = None) -> Path:
    package = root / "items" / "xiaoe" / identity
    (package / "notes").mkdir(parents=True)
    (package / "source").mkdir()
    (package / "notes" / "notes.md").write_text(
        note_text or "# 距离计算\n\n## 坐标转换\n\n机器人根据目标框中心计算距离。\n",
        encoding="utf-8",
    )
    cues = []
    for index, start in enumerate(range(0, 120, 10), 1):
        cues.append(
            f"{index}\n00:{start // 60:02d}:{start % 60:02d},000 --> "
            f"00:{(start + 8) // 60:02d}:{(start + 8) % 60:02d},000\n"
            f"第{index}段 机器人与目标框距离\n"
        )
    (package / "source" / "transcript.srt").write_text("\n".join(cues), encoding="utf-8")
    atomic_write_json(
        package / "manifest.json",
        {
            "schema_version": 4,
            "platform": "xiaoe",
            "identity": identity,
            "title": "机器人与目标框的距离计算",
            "collection": "小沫ROS智能体机器人课程",
            "section": "第十六章",
            "ordinal": 9,
            "topics": ["ROS", "目标跟踪"],
            "duration_seconds": 120,
            "artifacts": {
                "notes": "notes/notes.md",
                "transcript_srt": "source/transcript.srt",
            },
        },
    )
    return package


class LibraryIndexTests(unittest.TestCase):
    def test_rebuild_supports_chinese_substring_search_and_filters(self):
        with tempfile.TemporaryDirectory() as raw:
            library = Path(raw)
            make_index_package(library)
            rebuilt = rebuild_library(library)
            self.assertTrue(rebuilt["ok"])
            results = search_library(
                "目标框中心",
                library,
                limit=5,
                platform="xiaoe",
                collection="小沫ROS智能体机器人课程",
                section="第十六章",
            )
            self.assertEqual(len(results["results"]), 1)
            self.assertEqual(results["results"][0]["item_id"], "v_robot")
            self.assertIn("目标框", results["results"][0]["snippet"])

    def test_transcript_chunks_retain_time_ranges_between_30_and_90_seconds(self):
        with tempfile.TemporaryDirectory() as raw:
            library = Path(raw)
            make_index_package(library)
            rebuild_library(library)
            results = search_library("机器人与目标框", library, limit=10)
            transcript = [x for x in results["results"] if x["kind"] == "transcript"]
            self.assertGreaterEqual(len(transcript), 2)
            for item in transcript[:-1]:
                self.assertGreaterEqual(item["end_seconds"] - item["start_seconds"], 30)
                self.assertLessEqual(item["end_seconds"] - item["start_seconds"], 90)

    def test_update_reindexes_only_when_source_hash_changes(self):
        with tempfile.TemporaryDirectory() as raw:
            library = Path(raw)
            package = make_index_package(library)
            rebuild_library(library)
            unchanged = update_package(package, library)
            self.assertFalse(unchanged["updated"])
            note = package / "notes" / "notes.md"
            note.write_text(note.read_text(encoding="utf-8") + "\n## 新知识\n\n占空比控制电机。\n", encoding="utf-8")
            changed = update_package(package, library)
            self.assertTrue(changed["updated"])
            self.assertEqual(search_library("占空比控制", library)["results"][0]["item_id"], "v_robot")

    def test_rebuild_removes_deleted_items_and_index_is_reconstructable(self):
        with tempfile.TemporaryDirectory() as raw:
            library = Path(raw)
            package = make_index_package(library)
            rebuild_library(library)
            for path in sorted(package.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            package.rmdir()
            rebuilt = rebuild_library(library)
            self.assertEqual(rebuilt["indexed_items"], 0)
            self.assertEqual(search_library("目标框中心", library)["results"], [])

    def test_status_reports_fresh_and_stale_sources(self):
        with tempfile.TemporaryDirectory() as raw:
            library = Path(raw)
            package = make_index_package(library)
            rebuild_library(library)
            fresh = library_status(library)
            self.assertEqual(fresh["stale_documents"], 0)
            self.assertEqual(fresh["missing_paths"], 0)
            (package / "notes" / "notes.md").write_text("已在索引外修改", encoding="utf-8")
            stale = library_status(library)
            self.assertGreater(stale["stale_documents"], 0)

    def test_chinese_query_falls_back_across_particles(self):
        with tempfile.TemporaryDirectory() as raw:
            library = Path(raw); make_index_package(library); rebuild_library(library)
            self.assertTrue(search_library("机器人目标框中心", library)["results"])


if __name__ == "__main__":
    unittest.main()
