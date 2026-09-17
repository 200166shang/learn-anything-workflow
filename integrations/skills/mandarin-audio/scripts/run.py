#!/usr/bin/env python3
"""Run or inspect Mandarin listening production in one managed package."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def audio_info(path: Path) -> dict:
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
        "format=duration,bit_rate:stream=sample_rate,channels,bit_rate", "-of", "json", str(path)], capture_output=True, text=True)
    if result.returncode:
        return {"valid": False}
    raw = json.loads(result.stdout); stream = (raw.get("streams") or [{}])[0]; fmt = raw.get("format", {})
    info = {"duration": float(fmt.get("duration") or 0), "sample_rate": int(stream.get("sample_rate") or 0),
            "channels": int(stream.get("channels") or 0), "bitrate": int(stream.get("bit_rate") or fmt.get("bit_rate") or 0)}
    info["valid"] = info["duration"] > 0 and info["sample_rate"] == 48000 and info["channels"] == 1 and 56000 <= info["bitrate"] <= 72000
    return info


def workspace(path: Path) -> dict:
    result = subprocess.run(["video-extract", "workspace", "show", "--workspace", str(path), "--json"], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path); parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--check", action="store_true"); parser.add_argument("--json", action="store_true")
    args = parser.parse_args(); package = args.package.expanduser().resolve()
    try:
        config = workspace(args.workspace.expanduser().resolve()); manifest = read_json(package / "manifest.json")
        tools = config.get("tools", {}).get("pyvideotrans", {})
        output_dir = package / "listening/zh-CN"; output = output_dir / "podcast.zh-CN.mp3"
        current = audio_info(output) if output.is_file() else {"valid": False}
        if current["valid"]:
            result = {"status": "complete", "package": str(package), "audio": str(output), "spec": current,
                      "report": str(output_dir / "production-report.json")}
        elif args.check:
            result = {"status": "pending", "package": str(package), "audio": str(output), "spec": current,
                      "dependencies": {"python": bool(tools.get("python") and Path(tools["python"]).is_file()),
                                       "cli": bool(tools.get("cli") and Path(tools["cli"]).is_file())}}
        else:
            artifacts = manifest.get("artifacts", {}); provenance = manifest.get("provenance", {})
            raw = artifacts.get("source_audio"); source = package / raw if raw else None
            if not source or not source.is_file():
                raise RuntimeError("managed source audio is required")
            language = str(provenance.get("source_audio", {}).get("language") or "unknown")
            native = language.casefold().replace("_", "-").startswith("zh") or provenance.get("source_audio", {}).get("kind") == "native_chinese_track"
            output_dir.mkdir(parents=True, exist_ok=True)
            if native:
                subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-vn", "-ar", "48000", "-ac", "1", "-b:a", "64k", str(output)], check=True)
                report = {"mode": "native", "source_artifact": raw, "output": "podcast.zh-CN.mp3", "spec": audio_info(output)}
                (output_dir / "production-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            elif language.casefold().replace("_", "-").split("-")[0] == "en":
                python, cli = Path(str(tools.get("python", ""))), Path(str(tools.get("cli", "")))
                if not python.is_file() or not cli.is_file(): raise RuntimeError("pyVideoTrans audio localization is not configured in workspace.toml")
                run_manifest = output_dir / "manifest.json"
                command = [str(python), str(cli), "--task", "podcast", "--resume", str(output_dir)] if run_manifest.is_file() else [str(python), str(cli), "--task", "podcast", "--name", str(source), "--output-dir", str(output_dir), "--podcast-profile", "alibaba-podcast-tts-throughput"]
                completed = subprocess.run(command, text=True)
                if completed.returncode: raise RuntimeError("pyVideoTrans audio localization failed; inspect its safe production report")
            else:
                raise RuntimeError(f"unsupported source language for Mandarin synthesis: {language}")
            final = audio_info(output)
            if not final["valid"]: raise RuntimeError("Mandarin MP3 did not satisfy the 48 kHz mono 64 kbps contract")
            result = {"status": "complete", "package": str(package), "audio": str(output), "spec": final,
                      "report": str(output_dir / "production-report.json")}
    except Exception as exc:
        result = {"status": "failed", "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else "\n".join(f"{k}: {v}" for k, v in result.items()))
    return 0 if result.get("status") in {"complete", "pending"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
