---
name: mandarin-audio
description: Create or resume a natural-paced Mandarin listening MP3 from a managed VideoLearning package containing native Chinese or English source audio; do not download media or upload the result.
---

# Mandarin audio

Use this skill only after media has been placed in a VideoLearning package. Run the bundled helper; it delegates native-track normalization, authorized audio localization, safe reuse, and recovery to the public `audio.mandarin` capability:

```text
python scripts/run.py PACKAGE --workspace WORKSPACE_TOML --json
```

Use `--check` for a read-only dependency and artifact check. The canonical result is `<PACKAGE>/listening/zh-CN/podcast.zh-CN.mp3`; report its safe `production-report.json` alongside it.

Prefer an extracted native Chinese track. It is normalized without cloud work or paid authorization. Otherwise continue only when provenance confirms English source audio and the user supplied a non-sensitive `authorization_ref`; pass it with `--authorization-ref`. The configured profile is the external compatibility literal `alibaba-podcast-tts-throughput`. For interrupted work, use the returned `operation_id` with `video-extract operation show/resume/reconcile`; never automatically retry an uncertain paid submission.

Completion requires an ffprobe-readable MP3 with positive duration, 48 kHz, mono, and approximately 64 kbps. Treat missing or unknown source language as unsupported before a cloud call.

Never read or expose `run.private.json`, provider credentials, cookies, tokens, signed URLs, or private intermediates. This skill does not acquire media, produce study notes, or publish to a remote service.
