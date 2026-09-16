"""Strict, rename-only workspace cutover planning and writer detection."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable


PIPELINE_PATTERN = re.compile(r"(?:^|\s)(?:video-extract|ffmpeg|ffprobe|yt-dlp|whisper)(?:\s|$)|(?:batch_process|prepare_learning|scan_course)\.py(?:\s|$)|-m\s+video_extract", re.I)
OBSIDIAN_PATTERN = re.compile(r"(?:^|/)Obsidian(?:\s|$| Helper)", re.I)


def parse_lsof(lines: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """Classify lsof output: r handles warn, w/u handles block."""
    warnings: list[dict[str, Any]] = []
    writers: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip() or line.startswith("COMMAND"):
            continue
        fields = line.split(None, 8)
        if len(fields) < 9:
            continue
        command, pid_text, _user, fd, _kind, _device, _size, _node, name = fields
        entry = {"pid": int(pid_text), "command": command, "fd": fd, "path": name}
        mode = fd[-1:] if fd[-1:] in {"r", "w", "u"} else ""
        if mode in {"w", "u"}:
            writers.append(entry)
        elif mode == "r":
            warnings.append(entry)
    return {"active_writers": writers, "read_only_handles": warnings}


def classify_processes(lines: Iterable[str], ignored_pids: set[int] | None = None) -> list[dict[str, Any]]:
    ignored_pids = ignored_pids or set()
    blockers = []
    for line in lines:
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)$", line)
        if not match:
            continue
        pid, ppid, command = int(match.group(1)), int(match.group(2)), match.group(3)
        if pid in ignored_pids:
            continue
        if OBSIDIAN_PATTERN.search(command) or PIPELINE_PATTERN.search(command):
            blockers.append({"pid": pid, "ppid": ppid, "command": command})
    return blockers


def inspect_activity(root: Path, ignored_pids: set[int] | None = None) -> dict[str, Any]:
    lsof = subprocess.run(["lsof", "+D", str(root)], capture_output=True, text=True)
    handles = parse_lsof(lsof.stdout.splitlines())
    ps = subprocess.run(["ps", "ax", "-o", "pid=,ppid=,command="], capture_output=True, text=True, check=True)
    processes = classify_processes(ps.stdout.splitlines(), ignored_pids)
    return {**handles, "blocking_processes": processes,
            "ok": not handles["active_writers"] and not processes}


def validate_moves(moves: list[dict[str, str]], target_device_anchor: Path) -> dict[str, Any]:
    seen_sources: set[Path] = set(); seen_targets: set[Path] = set()
    conflicts = missing = exdev = duplicates = 0
    details = []
    target_dev = target_device_anchor.stat().st_dev
    for item in moves:
        source, target = Path(item["source"]), Path(item["target"])
        if source in seen_sources or target in seen_targets: duplicates += 1
        seen_sources.add(source); seen_targets.add(target)
        exists = source.exists(); collision = target.exists()
        source_dev = source.stat().st_dev if exists else None
        same_device = source_dev == target_dev if exists else False
        missing += not exists; conflicts += collision; exdev += exists and not same_device
        details.append({**item, "exists": exists, "target_exists": collision, "source_dev": source_dev, "target_dev": target_dev, "same_device": same_device})
    return {"ok": not any((conflicts, missing, exdev, duplicates)), "planned_moves": len(moves),
            "conflicts": conflicts, "missing": missing, "exdev": exdev, "duplicates": duplicates,
            "source_exactly_once": duplicates == 0 and len(seen_sources) == len(moves), "moves": details}
