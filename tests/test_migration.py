import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from video_extract.migration import build_plan, consolidate_plans, execute_plan


def write_package(root: Path, platform: str = "bilibili", identity: str = "BV1abc_p1", schema: int = 4) -> Path:
    package = root / "lesson"
    (package / "source").mkdir(parents=True)
    (package / "source/source.mp4").write_bytes(b"video")
    (package / "source/transcript.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
    (package / "source/metadata.json").write_text(json.dumps({"source_url": f"https://www.bilibili.com/video/{identity.split('_')[0]}/"}), encoding="utf-8")
    (package / "manifest.json").write_text(json.dumps({
        "schema_version": schema, "platform": platform, "identity": identity, "title": "lesson",
        "artifacts": {"video": "source/source.mp4", "metadata": "source/metadata.json", "transcript_srt": "source/transcript.srt"},
    }), encoding="utf-8")
    return package


class MigrationTests(unittest.TestCase):
    def test_plan_is_read_only_and_all_files_are_classified(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "source"; library = root / "library"
            package = write_package(source)
            before = {p.relative_to(source): (p.stat().st_ino, p.stat().st_size) for p in source.rglob("*") if p.is_file()}
            report = build_plan(source, library)
            after = {p.relative_to(source): (p.stat().st_ino, p.stat().st_size) for p in source.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(report["summary"]["unclassified"], 0)
            self.assertTrue(all(x["classification"] in {"move", "reuse_duplicate", "derived_ignore", "quarantine", "conflict"} for x in report["operations"]))
            self.assertTrue(package.exists())

    def test_move_uses_rename_and_preserves_inode(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "source"; library = root / "library"
            package = write_package(source); video = package / "source/source.mp4"; inode = video.stat().st_ino
            report = build_plan(source, library)
            result = execute_plan(report, root / "journal.jsonl")
            target = library / "items/bilibili/BV1abc_p1/source/source.mp4"
            self.assertTrue(result["ok"]); self.assertFalse(video.exists())
            self.assertEqual(target.stat().st_ino, inode)
            events = [json.loads(x) for x in (root / "journal.jsonl").read_text().splitlines()]
            self.assertIn("planned", {x["event"] for x in events}); self.assertIn("moved", {x["event"] for x in events})
            moved = next(x for x in events if x["event"] == "moved" and x["source"].endswith("source.mp4"))
            self.assertEqual(Path(moved["reverse"]["source"]).resolve(), target.resolve())

    def test_schema_one_is_normalized_to_schema_four(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "source"; library = root / "library"
            write_package(source, schema=1)
            result = execute_plan(build_plan(source, library), root / "journal.jsonl")
            manifest = json.loads((library / "items/bilibili/BV1abc_p1/manifest.json").read_text())
            self.assertTrue(result["ok"]); self.assertEqual(manifest["schema_version"], 4)

    def test_schema_five_is_preserved(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "source"; library = root / "library"
            package = write_package(source, platform="youtube", identity="abcdefghijk", schema=5)
            manifest = json.loads((package / "manifest.json").read_text()); manifest["request"] = {"type": "media"}
            (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            execute_plan(build_plan(source, library), root / "journal.jsonl")
            moved = json.loads((library / "items/youtube/abcdefghijk/manifest.json").read_text())
            self.assertEqual(moved["schema_version"], 5)

    def test_xiaoe_catalog_identity_wins_over_bad_local_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "source"; library = root / "library"; course = source / "xiaoe/课程"; lesson = course / "第一课"
            lesson.mkdir(parents=True); (lesson / "第一课.mp4").write_bytes(b"video")
            (lesson / "manifest.json").write_text(json.dumps({"schema_version": 4, "platform": "local", "identity": "/old/path/第一课.mp4", "title": "第一课", "artifacts": {"video": "第一课.mp4"}}), encoding="utf-8")
            (course / "course_catalog.json").write_text(json.dumps({"course_title": "课程", "course_url": "https://x/p?product_id=course_1", "lessons": [{"title": "第一课", "video_id": "v_123", "section": "第一章", "sort_value": 1, "is_video": True}]}), encoding="utf-8")
            report = build_plan(source, library)
            targets = {x.get("destination", "") for x in report["operations"] if x["classification"] == "move"}
            self.assertTrue(any("/items/xiaoe/v_123/" in x for x in targets))

    def test_local_identity_is_content_hash(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "source"; library = root / "library"; package = source / "local"
            package.mkdir(parents=True); (package / "clip.mp4").write_bytes(b"unique-video")
            report = build_plan(source, library)
            item_ids = {x.get("item_id") for x in report["operations"] if x.get("item_id")}
            self.assertEqual(len(item_ids), 1); self.assertEqual(len(next(iter(item_ids))), 64)

    def test_duplicate_is_reused_only_after_hash_match(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "source"; library = root / "library"
            package = write_package(source); target = library / "items/bilibili/BV1abc_p1/source/source.mp4"
            target.parent.mkdir(parents=True); target.write_bytes((package / "source/source.mp4").read_bytes())
            report = build_plan(source, library)
            duplicate = next(x for x in report["operations"] if x["source"].endswith("source.mp4"))
            self.assertEqual(duplicate["classification"], "reuse_duplicate")
            execute_plan(report, root / "journal.jsonl")
            self.assertFalse((package / "source/source.mp4").exists()); self.assertTrue(target.exists())

    def test_different_target_is_conflict_and_nothing_moves(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "source"; library = root / "library"
            package = write_package(source); target = library / "items/bilibili/BV1abc_p1/source/source.mp4"
            target.parent.mkdir(parents=True); target.write_bytes(b"different")
            report = build_plan(source, library)
            self.assertGreater(report["summary"]["conflict"], 0)
            with self.assertRaises(RuntimeError): execute_plan(report, root / "journal.jsonl")
            self.assertTrue((package / "source/source.mp4").exists()); self.assertEqual(target.read_bytes(), b"different")

    def test_exdev_stops_without_copy(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "source"; library = root / "library"; package = write_package(source)
            report = build_plan(source, library)
            with mock.patch("video_extract.migration.os.rename", side_effect=OSError(18, "Cross-device link")):
                with self.assertRaises(RuntimeError): execute_plan(report, root / "journal.jsonl")
            self.assertTrue((package / "source/source.mp4").exists())

    def test_manifest_path_escape_is_conflict(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "source"; library = root / "library"; package = write_package(source)
            manifest = json.loads((package / "manifest.json").read_text()); manifest["artifacts"]["video"] = "../../escape.mp4"
            (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            report = build_plan(source, library)
            self.assertGreater(report["summary"]["conflict"], 0)

    def test_second_plan_has_no_moves(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "source"; library = root / "library"
            write_package(source); execute_plan(build_plan(source, library), root / "journal.jsonl")
            again = build_plan(source, library)
            self.assertEqual(again["summary"]["move"], 0)

    def test_audited_duplicate_notes_keep_newer_normalized_and_variant(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "source"; library = root / "library"
            lesson = source / "xiaoe/课程/15.1"
            (lesson / "notes").mkdir(parents=True)
            (lesson / "notes.md").write_text("older", encoding="utf-8")
            (lesson / "notes/notes.md").write_text("newer", encoding="utf-8")
            manifest = {"schema_version": 4, "platform": "xiaoe", "identity": "v_fixture", "title": "15.1", "artifacts": {"notes": "notes/notes.md"}}
            (lesson / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            report = build_plan(source, library)
            notes = [x for x in report["operations"] if x["source"].endswith("notes.md")]
            self.assertEqual(report["summary"]["conflict"], 0)
            canonical = next(x for x in notes if x["source"].endswith("notes/notes.md"))
            variant = next(x for x in notes if x["source"].endswith("15.1/notes.md"))
            self.assertTrue(canonical["destination"].endswith("items/xiaoe/v_fixture/notes/notes.md"))
            self.assertIn("/legacy-variants/", variant["destination"])

    def test_ds_store_collision_is_quarantined_not_deleted(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "source"; library = root / "library"
            for name, content in (("a", b"one"), ("b", b"two")):
                package = source / name; package.mkdir(parents=True)
                (package / ".DS_Store").write_bytes(content)
                (package / "manifest.json").write_text(json.dumps({"schema_version": 4, "platform": "bilibili", "identity": "BV1abc_p1", "title": name, "artifacts": {}}), encoding="utf-8")
            report = build_plan(source, library)
            stores = [x for x in report["operations"] if x["source"].endswith(".DS_Store")]
            self.assertTrue(all(x["classification"] == "quarantine" for x in stores))
            self.assertEqual(len({x["destination"] for x in stores}), 2)

    def test_audited_existing_youtube_manifest_is_preserved_as_variant(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "output"; library = root / "library"; package = source / "jwChiek_aRY"
            package.mkdir(parents=True)
            incoming = {"schema_version": 5, "platform": "youtube", "identity": "jwChiek_aRY", "request": {"type": "media", "kinds": ["audio"], "language": "original"}, "artifacts": {}}
            (package / "manifest.json").write_text(json.dumps(incoming), encoding="utf-8")
            target = library / "items/youtube/jwChiek_aRY/manifest.json"; target.parent.mkdir(parents=True)
            target.write_text(json.dumps({**incoming, "provenance": {"source_audio": {"kind": "source_audio"}}}), encoding="utf-8")
            report = build_plan(source, library)
            operation = next(x for x in report["operations"] if x["source"].endswith("manifest.json"))
            self.assertEqual(report["summary"]["conflict"], 0)
            self.assertIn("/legacy-variants/", operation["destination"])

    def test_tts_preview_is_preserved_in_derived_quarantine(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "output"; library = root / "library"; source.mkdir()
            preview = source / "tencent-tts-preview-feijing.mp3"; preview.write_bytes(b"preview")
            report = build_plan(source, library)
            operation = report["operations"][0]
            self.assertEqual(operation["classification"], "quarantine")
            self.assertIn("/derived-preserved/", operation["destination"])

    def test_consolidation_accounts_each_source_once_and_preserves_variant(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); library = root / "library"
            sources = []
            for base, content in ((root / "obsidian_本地知识库/a", b"zh"), (root / "work/b", b"en")):
                path = base / "article.md"; path.parent.mkdir(parents=True); path.write_bytes(content); sources.append(path)
            destination = library / "items/bilibili/BV1zUh56RE8k_p1/source/article.md"
            reports=[]
            for path in sources:
                st=path.stat(); reports.append({"source_root":str(path.parent),"library_root":str(library),"devices":{"source":st.st_dev,"library":st.st_dev},"packages":[],"operations":[{"source":str(path),"relative_source":path.name,"source_stat":{"dev":st.st_dev,"ino":st.st_ino,"size":st.st_size,"mtime_ns":st.st_mtime_ns},"classification":"move","destination":str(destination)}]})
            result=consolidate_plans(reports)
            self.assertEqual(result["unique_sources"],2); self.assertEqual(result["summary"]["conflict"],0)
            self.assertEqual(sum("legacy-variants" in x["destination"] for x in result["operations"]),1)


if __name__ == "__main__":
    unittest.main()
