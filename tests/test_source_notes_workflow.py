import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from video_extract.manifest import atomic_write_json
from video_extract.notes_workflow import _prepare_video, finalize, prepare
from video_extract.source_import import import_source
from video_extract.workspace import WorkspaceConfig


def make_workspace(root: Path) -> WorkspaceConfig:
    (root / "project").mkdir(); (root / "media").mkdir(); (root / "vault").mkdir()
    config = root / "workspace.toml"
    config.write_text('''schema_version = 1
[paths]
project = "project"
media = "media"
obsidian = "vault"
[obsidian]
generated = "generated"
threads = "threads"
concepts = "concepts"
review = "REVIEW.md"
''', encoding="utf-8")
    return WorkspaceConfig.load(config)


class SourceNotesWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name); self.workspace = make_workspace(self.root)

    def tearDown(self): self.temp.cleanup()

    def test_document_import_prepare_and_finalize_stays_in_workspace(self):
        source = self.root / "input.md"; source.write_text("# 原理\n\n输入通过阶段变换产生输出。\n", encoding="utf-8")
        imported = import_source(source, self.workspace)
        package = Path(imported["package"])
        first = prepare(package, self.workspace)
        self.assertEqual((first["status"], first["action"]), ("awaiting_ai", "notes_write"))
        note = package / first["output"]
        note.write_text("# 原理笔记\n\n来源：# 原理\n\n阶段变换解释了输入如何产生输出。\n", encoding="utf-8")
        self.assertEqual(prepare(package, self.workspace)["status"], "ready")
        completed = finalize(package, self.workspace)
        self.assertEqual(completed["status"], "complete")
        self.assertTrue(Path(completed["export"]["note"]).is_file())
        self.assertTrue((self.workspace.media / "catalog/library.sqlite").is_file())
        self.assertFalse((package / "media/video.mp4").exists())
        self.assertFalse((package / "notes/notes_input.md").exists())
        self.assertTrue(completed["validation"]["ok"])

    def test_srt_never_invokes_asr_or_creates_video(self):
        source = self.root / "source.srt"
        source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        package = Path(import_source(source, self.workspace)["package"])
        result = prepare(package, self.workspace)
        self.assertEqual(result["action"], "notes_write")
        self.assertFalse((package / "media/video.mp4").exists())
        self.assertFalse((package / "media/audio.source.m4a").exists())

    def test_workspace_resolves_optional_pyvideotrans_paths(self):
        config = self.root / "workspace.toml"
        config.write_text(config.read_text(encoding="utf-8") + '\n[tools.pyvideotrans]\npython = "tools/python"\ncli = "tools/cli.py"\n', encoding="utf-8")
        workspace = WorkspaceConfig.load(config)
        self.assertEqual(workspace.pyvideotrans_python, workspace.root / "tools/python")
        self.assertEqual(workspace.as_dict()["tools"]["pyvideotrans"]["cli"], str(workspace.root / "tools/cli.py"))

    def test_video_candidate_outputs_are_normalized_under_evidence(self):
        package = self.workspace.media / "items/local/video"; video = package / "media/video.mp4"; transcript = package / "subtitles/transcript.source.srt"
        video.parent.mkdir(parents=True); transcript.parent.mkdir(parents=True)
        video.write_bytes(b"fixture"); transcript.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        data = {"artifacts": {"source_video": "media/video.mp4"}}
        def fake_run(*_args, **_kwargs):
            image = package / "frames/keyframes/candidate_001.jpg"; image.parent.mkdir(parents=True)
            Image.new("RGB", (4, 4), "red").save(image)
            Image.new("RGB", (4, 4), "blue").save(package / "frames/contact_sheet.jpg")
            atomic_write_json(package / "review/keyframes.json", {"candidates": [{"id": "frame_001", "timestamp": 0.2, "image": "frames/keyframes/candidate_001.jpg"}]})
            atomic_write_json(package / "review/approved_keyframes.json", {"review_mode": "pending", "approved": []})
            notes = package / "notes/notes_input.md"; notes.parent.mkdir(parents=True); notes.write_text("../frames/keyframes/candidate_001.jpg", encoding="utf-8")
            return type("Completed", (), {"returncode": 0, "stderr": "", "stdout": ""})()
        with patch("video_extract.notes_workflow.subprocess.run", side_effect=fake_run):
            _prepare_video(package, data, transcript)
        self.assertEqual(data["artifacts"]["candidate_json"], "evidence/candidates.json")
        self.assertTrue((package / "evidence/images/candidate_001.jpg").is_file())
        self.assertIn("../evidence/images/candidate_001.jpg", (package / "notes/notes_input.md").read_text())


if __name__ == "__main__": unittest.main()
