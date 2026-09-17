#!/usr/bin/env python3
"""Thin Skill helper for the public audio.mandarin capability."""
from __future__ import annotations
import argparse, hashlib, json, subprocess, tempfile
from pathlib import Path

def _source_version(package: Path) -> str:
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    values = (manifest.get("source_version"), manifest.get("provenance", {}).get("source_audio", {}).get("source_version"))
    for value in values:
        if isinstance(value, str) and value: return value
    raw = manifest.get("artifacts", {}).get("source_audio")
    if not isinstance(raw, str) or not (package / raw).is_file(): raise RuntimeError("prepared public source_audio is required")
    return "source-version-sha256-" + hashlib.sha256((package / raw).read_bytes()).hexdigest()

def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("package", type=Path)
    parser.add_argument("--workspace", type=Path, required=True); parser.add_argument("--authorization-ref")
    parser.add_argument("--adapter"); parser.add_argument("--check", action="store_true"); parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.check:
        command = ["video-extract", "capability", "check", "audio.mandarin", "--json"]
        completed = subprocess.run(command, text=True, capture_output=True)
    else:
        package = args.package.expanduser().resolve()
        request = {"contract_version": 1, "workspace": str(args.workspace.expanduser().resolve()), "package": str(package),
                   "source_version": _source_version(package), "profile": "alibaba-podcast-tts-throughput"}
        if args.authorization_ref: request["authorization_ref"] = args.authorization_ref
        if args.adapter: request["adapter"] = args.adapter
        temporary = tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False)
        try:
            json.dump(request, temporary); temporary.close()
            completed = subprocess.run(["video-extract", "capability", "run", "audio.mandarin", "--request", temporary.name, "--json"], text=True, capture_output=True)
        finally: Path(temporary.name).unlink(missing_ok=True)
    print(completed.stdout, end="")
    return completed.returncode

if __name__ == "__main__": raise SystemExit(main())
