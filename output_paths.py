"""The local, user-owned root for generated video-learning packages."""

from __future__ import annotations

import os
from pathlib import Path


def output_root() -> Path:
    """Return the package root, allowing a temporary shell override when needed."""
    override = os.environ.get("VIDEO_EXTRACT_OUTPUT_ROOT")
    if override: return Path(override).expanduser()
    from video_extract.workspace import discover_workspace
    return discover_workspace().generated


def platform_output_root(platform: str) -> Path:
    return output_root() / platform
