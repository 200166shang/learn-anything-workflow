"""Versioned lifecycle contract shared by commands and validators."""

SCHEMA_VERSION = 4  # legacy goal workflow; new media requests write schema v5 explicitly
MEDIA_SCHEMA_VERSION = 5
ITEM_GATES = ("media_ready", "transcript_ready", "candidates_ready", "evidence_selected", "notes_complete")
COLLECTION_GATES = ("catalog_ready", "summary_complete")
ALL_GATES = COLLECTION_GATES[:1] + ITEM_GATES + COLLECTION_GATES[1:]

LEGACY_GATES = {
    "completed": "notes_complete",
    "ready_for_notes": "evidence_selected",
    "evidence_ready": "evidence_selected",
    "ready_for_review": "candidates_ready",
    "transcript_ready": "transcript_ready",
    "media_ready": "media_ready",
    "downloaded": "media_ready",
}

NEXT_ITEM_GATE = {
    None: "media_ready",
    "media_ready": "transcript_ready",
    "transcript_ready": "candidates_ready",
    "candidates_ready": "evidence_selected",
    "evidence_selected": "notes_complete",
    "notes_complete": None,
}

DEFAULT_ARTIFACTS = {
    "video": "source/source.mp4",
    "source_video": "source/source.mp4",
    "source_audio": "source/source.m4a",
    "metadata": "source/metadata.json",
    "transcript_srt": "source/transcript.srt",
    "candidate_json": "review/keyframes.json",
    "contact_sheet": "frames/contact_sheet.jpg",
    "approved_json": "review/approved_keyframes.json",
    "notes_input": "notes/notes_input.md",
    "notes": "notes/notes.md",
    "translation_batches": "translation/batches.json",
    "localized_script": "translation/localized_script.json",
    "localized_audio": "audio/podcast.zh-CN.m4a",
}

V5_ARTIFACTS = {
    "source_video": "media/video.mp4", "source_audio": "media/audio.source.m4a",
    "source_subtitle": "subtitles/source.srt", "chinese_subtitle": "subtitles/zh-CN.srt",
    "localized_audio": "media/audio.zh-CN.m4a",
    "candidate_json": "evidence/candidates.json", "approved_json": "evidence/approved.json",
    "contact_sheet": "evidence/contact-sheet.jpg", "notes": "notes/notes.zh-CN.md",
}

GOALS = ("podcast_zh", "notes_zh")
GOAL_ARTIFACTS = {
    "podcast_zh": ("localized_audio",),
    "notes_zh": ("transcript_srt", "approved_json", "notes"),
}
