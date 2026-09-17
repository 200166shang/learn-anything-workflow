"""Canonical schema-v5 library paths without moving legacy packages."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .media import MediaInventory

def library_root() -> Path:
    override = os.environ.get("VIDEO_EXTRACT_LIBRARY_ROOT") or os.environ.get("VIDEO_EXTRACT_OUTPUT_ROOT")
    if override: return Path(override).expanduser()
    from .workspace import discover_workspace
    return discover_workspace().media


def obsidian_root() -> Path:
    override = os.environ.get("VIDEO_EXTRACT_OBSIDIAN_ROOT")
    if override: return Path(override).expanduser()
    from .workspace import discover_workspace
    return discover_workspace().generated


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "unknown"


def canonical_identity(inventory: MediaInventory) -> str:
    if inventory.platform == "local":
        path = Path(inventory.source or inventory.identity).expanduser()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""): digest.update(chunk)
        return digest.hexdigest()
    identity = inventory.identity
    if inventory.platform == "bilibili":
        page = re.search(r"(?:[?&]p=|_p)(\d+)", inventory.source or "")
        if page and not re.search(r"_p\d+$", identity): identity += f"_p{page.group(1)}"
    return _safe(identity)


def canonical_package(inventory: MediaInventory, root: Path | None = None) -> Path:
    return (root or library_root()) / "items" / inventory.platform / canonical_identity(inventory)
