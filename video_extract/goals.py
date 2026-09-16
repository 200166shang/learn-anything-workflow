"""Public goal request types and normalization."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Iterable


class Goal(str, Enum):
    PODCAST_ZH = "podcast_zh"
    NOTES_ZH = "notes_zh"


class KeepVideo(str, Enum):
    AUTO = "auto"
    YES = "yes"
    NO = "no"


class AudioQuality(str, Enum):
    STANDARD = "standard"
    HIGH = "high"


@dataclass(frozen=True)
class GoalRequest:
    goals: tuple[Goal, ...]
    keep_video: KeepVideo = KeepVideo.AUTO
    audio_quality: AudioQuality = AudioQuality.HIGH
    video_quality: int | None = None
    voice: str = "Tingting"

    def __post_init__(self) -> None:
        if not self.goals:
            raise ValueError("at least one goal is required")
        if len(set(self.goals)) != len(self.goals):
            raise ValueError("duplicate goals are not allowed")
        quality = self.resolved_video_quality
        if quality < 144 or quality > 4320:
            raise ValueError("video quality must be between 144 and 4320")
        if not self.voice.strip():
            raise ValueError("voice cannot be empty")

    @property
    def resolved_video_quality(self) -> int:
        return self.video_quality or (720 if self.keep_video is KeepVideo.YES else 480)

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["goals"] = [goal.value for goal in self.goals]
        value["keep_video"] = self.keep_video.value
        value["audio_quality"] = self.audio_quality.value
        value["video_quality"] = self.resolved_video_quality
        return value


def normalize_request(
    goals: Iterable[str | Goal], keep_video: str | KeepVideo = "auto",
    audio_quality: str | AudioQuality = "high", video_quality: int | None = None,
    voice: str = "Tingting",
) -> GoalRequest:
    try:
        normalized = tuple(goal if isinstance(goal, Goal) else Goal(goal) for goal in goals)
        retention = keep_video if isinstance(keep_video, KeepVideo) else KeepVideo(keep_video)
        audio = audio_quality if isinstance(audio_quality, AudioQuality) else AudioQuality(audio_quality)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    return GoalRequest(normalized, retention, audio, video_quality, voice)
