"""Schema-v5 media extraction request contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum


class MediaKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLES = "subtitles"


@dataclass(frozen=True)
class MediaRequest:
    kinds: tuple[MediaKind, ...]
    language: str = "original"
    quality: str = "high"
    retention: str = "normal"

    def to_dict(self):
        value = asdict(self); value["kinds"] = [x.value for x in self.kinds]
        return value


def normalize_media_request(output: str = "all", language: str = "original", quality: str = "high", retention: str = "normal") -> MediaRequest:
    kinds = tuple(MediaKind) if output == "all" else (MediaKind(output),)
    if not language.strip(): raise ValueError("language cannot be empty")
    if quality not in {"balanced", "high", "standard"}: raise ValueError("unsupported quality")
    return MediaRequest(kinds, language, quality, retention)
