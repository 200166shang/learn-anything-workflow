from pathlib import Path
from video_extract.media_workflow import _existing

def test_existing_artifacts_map_to_planner_capabilities(tmp_path: Path):
    audio=tmp_path/"media/audio.source.m4a"; audio.parent.mkdir(); audio.write_bytes(b"x")
    data={"artifacts":{"source_audio":"media/audio.source.m4a"},"provenance":{}}
    assert _existing(tmp_path,data)=={"source_audio_ready"}
