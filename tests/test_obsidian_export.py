import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from video_extract.manifest import atomic_write_json
from video_extract.obsidian_export import export_all_verified, export_package


def make_notes_package(root: Path, identity: str = "v_fixture", verified: bool = True) -> Path:
    package = root / "items" / "xiaoe" / identity
    (package / "source").mkdir(parents=True)
    (package / "notes").mkdir()
    (package / "review").mkdir()
    (package / "frames" / "keyframes").mkdir(parents=True)
    (package / "source" / "transcript.srt").write_text(
        "1\n00:00:01,000 --> 00:00:04,000\n机器人目标距离计算\n",
        encoding="utf-8",
    )
    adopted = package / "frames" / "keyframes" / "adopted.jpg"
    rejected = package / "frames" / "keyframes" / "rejected.jpg"
    Image.new("RGB", (8, 8), "red").save(adopted)
    Image.new("RGB", (8, 8), "blue").save(rejected)
    atomic_write_json(
        package / "review" / "approved_keyframes.json",
        {
            "review_mode": "model_only",
            "approved_by": "fixture",
            "approved": [
                {
                    "id": "frame-1",
                    "timestamp": 2.0,
                    "image": "frames/keyframes/adopted.jpg",
                }
            ],
        },
    )
    note = "# 距离计算\n\n## 核心知识点\n\n机器人会在 00:02 计算距离。\n\n![证据](../frames/keyframes/adopted.jpg)\n"
    if not verified:
        note = "# 未完成\n"
    (package / "notes" / "notes.md").write_text(note, encoding="utf-8")
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
                "source_video": "source/source.mp4",
                "transcript_srt": "source/transcript.srt",
                "approved_json": "review/approved_keyframes.json",
                "notes": "notes/notes.md",
            },
            "request": {"goals": ["notes_zh"]},
        },
    )
    return package


class ObsidianExportTests(unittest.TestCase):
    def test_rejects_package_that_does_not_verify(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package = make_notes_package(root / "library", verified=False)
            result = export_package(package, root / "obsidian")
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "validation_failed")
            self.assertFalse((root / "obsidian").exists())

    def test_exports_properties_adopted_images_and_bases(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package = make_notes_package(root / "library")
            result = export_package(package, root / "obsidian")
            self.assertTrue(result["ok"])
            exported = Path(result["note"])
            text = exported.read_text(encoding="utf-8")
            self.assertIn('item_id: "v_fixture"', text)
            self.assertIn('collection: "小沫ROS智能体机器人课程"', text)
            self.assertIn('topics: ["ROS", "目标跟踪"]', text)
            self.assertIn('migration_status: "migrated"', text)
            self.assertIn('validation_status: "valid_complete"', text)
            self.assertIn("images/adopted.jpg", text)
            self.assertIn("file://", text)
            self.assertEqual([x.name for x in (exported.parent / "images").iterdir()], ["adopted.jpg"])
            self.assertFalse((exported.parent / "images" / "rejected.jpg").exists())
            self.assertTrue((package / "export-manifest.json").is_file())
            table = (root / "obsidian" / "视频资源.base").read_text(encoding="utf-8")
            cards = (root / "obsidian" / "课程浏览.base").read_text(encoding="utf-8")
            self.assertIn("type: table", table)
            self.assertIn("note.collection", table)
            self.assertIn("type: cards", cards)
            self.assertIn("groupBy", cards)
            self.assertIn("direction: ASC", cards)
            self.assertIn('file.inFolder(this.file.folder)', cards)
            self.assertIn('file.inFolder(this.file.folder)', table)

    def test_manual_edit_is_reported_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package = make_notes_package(root / "library")
            first = export_package(package, root / "obsidian")
            exported = Path(first["note"])
            exported.write_text(exported.read_text(encoding="utf-8") + "\n人工补充\n", encoding="utf-8")
            (package / "notes" / "notes.md").write_text(
                (package / "notes" / "notes.md").read_text(encoding="utf-8") + "\n新的源内容\n",
                encoding="utf-8",
            )
            result = export_package(package, root / "obsidian")
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "conflict")
            self.assertIn("人工补充", exported.read_text(encoding="utf-8"))

    def test_unchanged_export_is_idempotently_reused(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package = make_notes_package(root / "library")
            first = export_package(package, root / "obsidian")
            exported = Path(first["note"])
            before = exported.stat().st_mtime_ns
            second = export_package(package, root / "obsidian")
            self.assertTrue(second["ok"])
            self.assertEqual(second["status"], "reused")
            self.assertEqual(exported.stat().st_mtime_ns, before)

    def test_export_all_marks_readable_unverified_packages(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            library = root / "library"
            make_notes_package(library, "v_verified")
            make_notes_package(library, "v_partial", verified=False)
            result = export_all_verified(library, root / "obsidian")
            self.assertTrue(result["ok"])
            self.assertEqual(result["exported"], 2)
            self.assertEqual(result["preserved_invalid"], 1)

    def test_preserved_invalid_export_keeps_existing_images_and_normalizes_space_entities(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package = make_notes_package(root / "library")
            approved_path = package / "review" / "approved_keyframes.json"
            approved = json.loads(approved_path.read_text(encoding="utf-8"))
            approved.pop("review_mode")
            atomic_write_json(approved_path, approved)
            notes_path = package / "notes" / "notes.md"
            notes_path.write_text(
                notes_path.read_text(encoding="utf-8")
                + "\nA&#x20;B&nbsp;C&#32;D\n\n![包根目录旧图片](keyframes/root-relative.jpg)\n",
                encoding="utf-8",
            )
            root_relative = package / "keyframes" / "root-relative.jpg"
            root_relative.parent.mkdir()
            Image.new("RGB", (8, 8), "green").save(root_relative)

            result = export_package(package, root / "obsidian", allow_preserved_invalid=True)

            self.assertTrue(result["ok"])
            exported = Path(result["note"])
            text = exported.read_text(encoding="utf-8")
            self.assertIn("![证据](images/adopted.jpg)", text)
            self.assertIn("![包根目录旧图片](images/root-relative.jpg)", text)
            self.assertNotIn("缺失的历史图片：adopted.jpg", text)
            self.assertIn("A B C D", text)
            self.assertNotIn("&#x20;", text)
            self.assertNotIn("&nbsp;", text)
            self.assertNotIn("&#32;", text)
            self.assertTrue((exported.parent / "images" / "adopted.jpg").is_file())
            self.assertTrue((exported.parent / "images" / "root-relative.jpg").is_file())


if __name__ == "__main__":
    unittest.main()
