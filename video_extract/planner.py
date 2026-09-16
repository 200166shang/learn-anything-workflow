"""Pure capability planning from a goal request and normalized inventory."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .goals import Goal, GoalRequest, KeepVideo
from .media import MediaInventory, select_audio_stream
from .media_request import MediaKind, MediaRequest


@dataclass(frozen=True)
class PlanStage:
    name: str
    capability: str
    reason: str
    pause: str | None = None


def build_plan(inventory: MediaInventory, request: GoalRequest, existing: Iterable[str] = ()) -> dict[str, Any]:
    have = set(existing); stages: list[PlanStage] = []
    def add(name: str, capability: str, reason: str, pause: str | None = None) -> None:
        if capability not in have and all(stage.capability != capability for stage in stages):
            stages.append(PlanStage(name, capability, reason, pause))
    add("resolve_source", "source_resolved", "normalize platform media inventory")
    wants_podcast = Goal.PODCAST_ZH in request.goals
    wants_notes = Goal.NOTES_ZH in request.goals
    native = select_audio_stream(inventory, request.audio_quality, "zh")
    source_audio = select_audio_stream(inventory, request.audio_quality)
    formal_subtitle = next((x for x in inventory.subtitles if not x.automatic), None)
    if wants_podcast:
        if native:
            add("normalize_native_chinese_audio", "localized_audio_ready", "native Chinese track bypasses translation and TTS")
        else:
            add("materialize_source_audio", "source_audio_ready", "prefer reusable or independent audio")
            if formal_subtitle: add("materialize_formal_subtitle", "transcript_ready", "authorized formal subtitle bypasses ASR")
            else: add("transcribe_source_audio", "transcript_ready", "no formal subtitle is available")
            add("translate_podcast_batches", "localized_script_ready", "complete spoken Chinese translation is required", "podcast_translate")
            add("synthesize_podcast", "localized_audio_ready", "render and concatenate validated Mandarin segments")
    if wants_notes:
        if not formal_subtitle:
            add("materialize_source_audio", "source_audio_ready", "audio is required for local ASR")
            add("transcribe_source_audio", "transcript_ready", "no formal subtitle is available")
        else: add("materialize_formal_subtitle", "transcript_ready", "reuse authorized formal subtitle")
        add("materialize_source_video", "source_video_ready", "visual evidence requires decodable frames")
        add("generate_candidates", "candidates_ready", "combine transcript anchors, intervals, and scene changes")
        add("select_evidence", "evidence_selected", "select conclusion-supporting visual evidence", "evidence_select")
        add("write_notes", "notes_complete", "write Chinese notes from transcript and selected evidence", "notes_write")
    exclusions = []
    if not wants_notes: exclusions += ["screenshots", "evidence_review", "notes"]
    if not wants_podcast: exclusions += ["translation", "tts", "localized_audio"]
    ephemeral = wants_notes and request.keep_video is not KeepVideo.YES and inventory.platform != "local" and "source_video" not in have
    selected_audio = native if wants_podcast and native else source_audio
    return {
        "platform": inventory.platform, "identity": inventory.identity, "title": inventory.title,
        "requested_goals": [goal.value for goal in request.goals], "parameters": request.to_dict(),
        "existing_reusable_artifacts": sorted(have), "planned_stages": [asdict(x) for x in stages],
        "exclusions": exclusions, "retention": {"keep_video": request.keep_video.value, "video_quality": request.resolved_video_quality, "ephemeral_proxy": ephemeral, "cleanup_after_verification": ephemeral},
        "selected_audio": ({"id": selected_audio.id, "language": selected_audio.language, "bitrate": selected_audio.bitrate, "muxed": selected_audio.muxed} if selected_audio else None),
        "ai_pauses": [x.pause for x in stages if x.pause],
        "blockers": ["persistent browser authorization is required"] if inventory.authorization_context == "persistent_browser_required" else [],
    }


def build_media_plan(inventory: MediaInventory, request: MediaRequest, existing: Iterable[str] = ()) -> dict[str, Any]:
    """Pure plan for extraction intent; it never introduces learning work."""
    have = set(existing); stages: list[PlanStage] = []
    def add(name: str, capability: str, reason: str) -> None:
        if capability not in have: stages.append(PlanStage(name, capability, reason))
    add("resolve_source", "source_resolved", "normalize platform media inventory")
    wants_video = MediaKind.VIDEO in request.kinds
    wants_audio = MediaKind.AUDIO in request.kinds
    wants_subtitles = MediaKind.SUBTITLES in request.kinds
    native = select_audio_stream(inventory, request.quality, "zh") if request.language.casefold().startswith("zh") else None
    source_audio = select_audio_stream(inventory, request.quality)
    if wants_video: add("materialize_source_video", "source_video_ready", "requested video")
    if wants_audio:
        if native: add("normalize_native_chinese_audio", "localized_audio_ready", "native Chinese audio satisfies request")
        elif request.language.casefold().startswith("zh"):
            source_language = inventory.original_language or (source_audio.language if source_audio else None) or next((x.language for x in inventory.audio_streams if x.language), None)
            if source_language and source_language.casefold().replace("_", "-").split("-")[0] == "en":
                add("materialize_source_audio", "source_audio_ready", "provide original English audio to external pyVideoTrans")
        else:
            add("materialize_source_audio", "source_audio_ready", "requested original audio")
    if wants_subtitles: add("materialize_best_subtitle", "source_subtitle_ready", "best official subtitle, or explicit unavailable result")
    mandarin_audio = None
    if wants_audio and request.language.casefold().startswith("zh"):
        source_language = inventory.original_language or (source_audio.language if source_audio else None) or next((x.language for x in inventory.audio_streams if x.language), None) or "unknown"
        if native:
            mandarin_audio = {"mode": "native_chinese", "source_language": native.language}
        elif source_language.casefold().replace("_", "-").split("-")[0] == "en":
            mandarin_audio = {"mode": "external_pyvideotrans", "source_language": source_language,
                              "input_artifact": "media/audio.source.m4a"}
        else:
            mandarin_audio = {"mode": "unsupported", "source_language": source_language,
                              "reason": "Chinese listening audio currently supports English source audio only"}
    return {"schema_version": 5, "platform": inventory.platform, "identity": inventory.identity,
            "request": request.to_dict(), "planned_stages": [asdict(x) for x in stages],
            "selected_audio": ({"id": source_audio.id, "language": source_audio.language} if source_audio and not native else None),
            "mandarin_audio": mandarin_audio,
            "blockers": ["persistent browser authorization is required"] if inventory.authorization_context == "persistent_browser_required" else []}
