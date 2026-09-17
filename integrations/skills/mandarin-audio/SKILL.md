---
name: mandarin-audio
description: Create or resume a natural-paced Mandarin listening MP3 from a managed VideoLearning package containing native Chinese or English source audio; do not download media or upload the result.
---

# Mandarin audio

Use this skill only after media has been placed in a VideoLearning package. Run the bundled helper; it owns native-track normalization, pyVideoTrans podcast invocation, safe reuse, and output checks:

```text
python scripts/run.py PACKAGE --workspace WORKSPACE_TOML --json
```

Use `--check` for a read-only dependency and artifact check. The canonical result is `<PACKAGE>/listening/zh-CN/podcast.zh-CN.mp3`; report its safe `production-report.json` alongside it.

Prefer an extracted native Chinese track. Normalize it without cloud work. Otherwise continue only when provenance confirms English source audio. The configured pyVideoTrans podcast profile is `alibaba-podcast-tts-throughput`. Resume an existing run directory; do not automatically retry an uncertain paid submission.

Completion requires an ffprobe-readable MP3 with positive duration, 48 kHz, mono, and approximately 64 kbps. Treat missing or unknown source language as unsupported before a cloud call.

Never read or expose `run.private.json`, provider credentials, cookies, tokens, signed URLs, or private intermediates. This skill does not acquire media, produce study notes, or publish to a remote service.
