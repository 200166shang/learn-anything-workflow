"""Podcast translation batching, validation, synthesis, and concatenation."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .manifest import atomic_write_json, fingerprint, read_json
from .tts import SpeechSynthesizer

SRT_CUE = re.compile(r"(?ms)^\s*(\d+)\s*\n(\d\d:\d\d:\d\d[,.]\d{3})\s+-->\s+(\d\d:\d\d:\d\d[,.]\d{3})[^\n]*\n(.*?)(?=\n\s*\n|\Z)")
CHINESE = re.compile(r"[\u3400-\u9fff]")


def _seconds(value: str) -> float:
    h, m, s = value.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_srt(path: Path) -> list[dict[str, Any]]:
    cues = []
    for number, start, end, text in SRT_CUE.findall(path.read_text(encoding="utf-8-sig")):
        clean = " ".join(line.strip() for line in text.splitlines() if line.strip())
        if clean: cues.append({"id": str(number), "start": _seconds(start), "end": _seconds(end), "source_text": clean})
    if not cues: raise ValueError("transcript has no timed cues")
    return cues


def prepare_translation_batches(transcript: Path, output: Path, max_seconds: float = 720, max_characters: int = 8000) -> dict[str, Any]:
    cues = parse_srt(transcript); batches = []; current = []
    for cue in cues:
        elapsed = cue["end"] - (current[0]["start"] if current else cue["start"])
        chars = sum(len(x["source_text"]) for x in current) + len(cue["source_text"])
        if current and (elapsed > max_seconds or chars > max_characters):
            batches.append(current); current = []
        current.append(cue)
    if current: batches.append(current)
    payload = {"schema": "podcast-translation-batches-v1", "source_fingerprint": fingerprint([transcript]), "batches": [
        {"id": f"batch-{i:04d}", "segment_ids": [x["id"] for x in batch], "start": batch[0]["start"], "end": batch[-1]["end"], "segments": batch}
        for i, batch in enumerate(batches, 1)]}
    atomic_write_json(output, payload); return payload


def validate_localized_script(script: dict[str, Any], batches: dict[str, Any]) -> list[str]:
    errors = []; expected = [sid for batch in batches.get("batches", []) for sid in batch.get("segment_ids", [])]
    segments = script.get("segments")
    if not isinstance(segments, list): return ["localized script segments must be a list"]
    actual = [str(x.get("id")) for x in segments if isinstance(x, dict)]
    if actual != expected: errors.append("localized script does not preserve complete source segment order")
    last_end = -1.0
    for entry in segments:
        if not isinstance(entry, dict): errors.append("localized segment must be an object"); continue
        start, end, text = entry.get("start"), entry.get("end"), entry.get("text")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or start < last_end or end <= start: errors.append(f"invalid timestamps for segment {entry.get('id')}")
        if not isinstance(text, str) or not text.strip() or not CHINESE.search(text): errors.append(f"segment {entry.get('id')} has no Chinese text")
        if isinstance(end, (int, float)): last_end = end
    return errors


def render_podcast(script_path: Path, batches_path: Path, output: Path, synthesizer: SpeechSynthesizer, voice: str, ffmpeg: str = "ffmpeg", audio_bitrate: str = "192k") -> dict[str, Any]:
    script, batches = read_json(script_path), read_json(batches_path); errors = validate_localized_script(script, batches)
    if errors: raise ValueError("; ".join(errors))
    segment_dir = output.parent / ".segments"; segment_dir.mkdir(parents=True, exist_ok=True); rendered = []
    for entry in script["segments"]:
        segment = segment_dir / f"{int(entry['id']):06d}.aiff"
        marker = segment.with_suffix(".fingerprint")
        expected = fingerprint([], {"text": entry["text"], "voice": voice})
        if not segment.exists() or not marker.exists() or marker.read_text() != expected:
            synthesizer.synthesize(entry["text"], segment, voice); marker.write_text(expected)
        rendered.append(segment)
    concat = segment_dir / "concat.txt"; concat.write_text("".join(f"file '{p.name}'\n" for p in rendered), encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True); temporary = output.with_name(f".{output.name}.partial.m4a")
    try:
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", audio_bitrate, "-movflags", "+faststart", str(temporary)], check=True)
        temporary.replace(output)
    finally: temporary.unlink(missing_ok=True)
    return {"kind": "synthesized", "voice": voice, "audio_bitrate": audio_bitrate, "segments": [str(x["id"]) for x in script["segments"]]}
