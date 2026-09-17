"""Speech synthesis port and deterministic/macOS implementations."""

from __future__ import annotations

import math
import struct
import subprocess
import wave
from abc import ABC, abstractmethod
from pathlib import Path


class SpeechSynthesizer(ABC):
    @abstractmethod
    def synthesize(self, text: str, output: Path, voice: str) -> None: ...


class MacOSSaySynthesizer(SpeechSynthesizer):
    def __init__(self, executable: str = "/usr/bin/say") -> None:
        self.executable = executable

    def synthesize(self, text: str, output: Path, voice: str = "Tingting") -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.partial.aiff")
        try:
            subprocess.run([self.executable, "-v", voice, "-o", str(temporary), text], check=True, capture_output=True)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)


class FakeSynthesizer(SpeechSynthesizer):
    """Create a short deterministic PCM WAV fixture for tests."""
    def synthesize(self, text: str, output: Path, voice: str = "test") -> None:
        output.parent.mkdir(parents=True, exist_ok=True); rate = 8000
        frames = max(rate // 20, min(rate, len(text.encode("utf-8")) * 80))
        with wave.open(str(output), "wb") as stream:
            stream.setparams((1, 2, rate, frames, "NONE", "not compressed"))
            stream.writeframes(b"".join(struct.pack("<h", int(700 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(frames)))
