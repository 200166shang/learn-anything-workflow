---
name: extract-media
description: Acquire authorized video, an existing audio track, or subtitles from YouTube, Bilibili, Xiaoe, or local media into a managed VideoLearning package; do not synthesize translated audio or write notes.
---

# Extract media

Resolve the workspace first and keep the returned config path fixed for the task:

```text
video-extract workspace show --json
video-extract plan SOURCE --media video|audio|subtitles|all --language original|LANGUAGE --workspace CONFIG --json
video-extract ensure SOURCE --media video|audio|subtitles|all --language original|LANGUAGE --workspace CONFIG --json
```

Use `--output PACKAGE` only for an explicitly supplied existing package. Otherwise let the tool select the canonical package. Reuse validated artifacts and resumable partials. A requested language means an existing source track; report `missing_requested_language_track` when absent.

Read [acquisition and recovery](references/acquisition.md) when acquisition is missing or blocked, then the matching platform reference. Stop at login, CAPTCHA, DRM, or paywall boundaries that need user action. Never expose credentials, cookies, browser profiles, authorization headers, tokens, or signed URLs.

Report the package and each requested media kind as produced or unavailable. This skill does not call another skill, generate Mandarin listening audio, perform ASR for notes, write notes, or publish files.
