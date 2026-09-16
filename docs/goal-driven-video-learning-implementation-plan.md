# Goal-driven video learning implementation plan

**Status:** approved and implemented on 2026-09-13  
**Execution agent:** `gpt-5.6-sol`, low reasoning  
**Verification:** 31 tests plus 8 subtests pass; dependency doctor passes. Acceptance covers the migrated `MGxcosNuC8k` native Chinese audio, a newly synthesized `ddq8JIMhz7c` Mandarin podcast, Bilibili notes with selected screenshots, and notes generated from an authorized local Xiaoe copy.

## 1. Decision

The public workflow exposes exactly two goals:

- `podcast_zh`: deliver a listenable Mandarin podcast audio file. It never requires screenshots or learning notes.
- `notes_zh`: deliver Chinese learning notes with timestamp evidence and selected visual evidence. It never requires Chinese dubbed audio.

All other states are internal capabilities used for planning, validation, caching, and recovery. Users do not select internal gates in normal use.

`keep_video` is a retention policy, not a third goal:

- `auto` (default): reuse and preserve an existing/local video; otherwise acquire a 480p working proxy for notes and remove it only after the requested goal verifies successfully.
- `yes`: retain a locally playable video; default maximum height is 720 unless the user specifies another quality.
- `no`: do not retain a newly acquired complete video after successful goal verification. Temporary media and resumable partials are preserved on failure.

“Do not keep the video” means no complete video is retained as a final artifact. Screenshot generation still transfers and decodes enough video data to obtain frames.

## 2. Public interface

Add goal-driven commands while retaining current low-level commands for backward compatibility:

```bash
video-extract plan SOURCE \
  --goal {podcast_zh,notes_zh} [--goal ...] \
  [--keep-video {auto,yes,no}] \
  [--audio-quality standard|high] \
  [--video-quality HEIGHT] \
  [--voice VOICE] \
  --json

video-extract ensure SOURCE \
  --goal {podcast_zh,notes_zh} [--goal ...] \
  [--keep-video {auto,yes,no}] \
  [--audio-quality standard|high] \
  [--video-quality HEIGHT] \
  [--voice VOICE] \
  --output PACKAGE_ROOT \
  --json

video-extract verify PACKAGE \
  [--goal {podcast_zh,notes_zh}] \
  --json

video-extract status PACKAGE --json
```

Defaults:

- `keep_video=auto`
- `audio_quality=high` for `podcast_zh`
- `video_quality=480` for an ephemeral notes proxy
- retained video maximum height `720`
- `voice=Tingting` on macOS for the first implementation
- notes language and podcast language are fixed to Simplified Chinese in these two goals

`plan` is read-only. It reports resolved platform, requested goals, existing reusable artifacts, planned stages, exclusions, retention result, AI pauses, and blockers.

`ensure` runs deterministic work until the goals verify or an explicit pause is required. It must never claim completion from a stored status label alone.

## 3. Interaction contract for the skills

The skill translates natural language into the two goals and the retention policy.

It asks one concise question before execution only when ambiguity materially changes:

- which of the two goals is wanted;
- single item versus playlist/course scope;
- whether a complete video should be retained when the wording implies local viewing but is unclear;
- voice choice when the user requests a special voice but does not identify one;
- use of a paid/external provider;
- login, CAPTCHA, DRM, or access-control handling.

It does not ask about subtitle selection, ASR fallback, stream selection, yt-dlp, ffmpeg, proxy quality, cache reuse, batching, or resume. The planner owns those decisions.

Before a write, the skill echoes one short interpretation, for example:

```text
将生成中文学习笔记并提取画面证据；新下载的视频只作临时处理，不长期保留。
```

When there is no material ambiguity, execution starts without a question.

## 4. Internal capability graph

The planner uses these internal capabilities; they are not normal user-facing targets:

```text
source_resolved
├── source_audio_ready
│   ├── transcript_ready
│   │   ├── localized_script_ready ── localized_audio_ready
│   │   └── with source_video_ready ── candidates_ready
│   │                                  └── evidence_selected
│   │                                      └── notes_complete
│   └── native_zh_audio_ready ───────── localized_audio_ready
└── source_video_ready
```

Goal satisfaction:

- `podcast_zh` is complete when `localized_audio_ready` validates.
- `notes_zh` is complete when `notes_complete` validates.
- A completed goal stays complete after an ephemeral source video is removed because goal validation checks final required artifacts, not whether every historical prerequisite still exists.

## 5. Planner rules

### 5.1 `podcast_zh`

1. Resolve platform metadata and available audio languages.
2. If a native Simplified Chinese track exists, download it directly and normalize it to M4A.
3. Otherwise obtain source audio using this order:
   1. reuse an existing package-local audio artifact;
   2. download an independent audio stream;
   3. stream-extract audio from a muxed remote stream without retaining video;
   4. extract audio from an existing local video;
   5. acquire a temporary muxed stream only when the adapter offers no audio-only path.
4. Prefer an authorized formal source-language subtitle for translation input. If none exists, run local faster-whisper transcription in the detected source language.
5. Pause for AI translation batches. Translation must preserve the complete spoken content, names, numbers, terminology, ordering, and uncertainty; it must produce natural spoken Chinese and must not summarize.
6. Render each validated Chinese segment with the TTS adapter, concatenate in order, normalize loudness, and write `audio/podcast.zh-CN.m4a`.
7. Verify the podcast goal. Do not generate screenshots, evidence review, or notes.

### 5.2 `notes_zh`

1. Resolve platform metadata.
2. Reuse an authorized formal subtitle when available; otherwise obtain source audio and run local faster-whisper transcription.
3. Reuse an existing/local video if present.
4. If no video exists, acquire according to `keep_video`:
   - `yes`: retain the requested/default-quality video;
   - `auto` or `no`: acquire a 480p working proxy.
5. Generate mixed screenshot candidates from transcript anchors, fixed intervals, and scene changes.
6. Pause for AI visual selection and record `review_mode=model_only`; preserve optional human review without requiring it by default.
7. Pause for AI note writing. GPT reads the source-language transcript directly and writes Chinese `notes/notes.md` with conclusion-level timestamp evidence and selected visual evidence.
8. Verify `notes_zh` before applying retention cleanup.
9. For a newly acquired proxy under `auto` or `no`, remove it only after successful goal verification. Never remove a pre-existing or local input video.
10. Do not generate Chinese dubbed audio unless `podcast_zh` was also requested.

### 5.3 Multiple goals

When both goals are requested, the planner shares source resolution, source audio, formal subtitles/ASR, metadata, and fingerprints. It independently validates the final podcast and notes artifacts.

## 6. Platform adapter seam

Introduce one internal media-source interface returning a normalized inventory:

```python
MediaInventory(
    platform,
    identity,
    title,
    duration,
    audio_streams,
    video_streams,
    subtitles,
    authorization_context,
)
```

Adapters:

- YouTube: yt-dlp metadata and stream selection.
- Bilibili: yt-dlp first, persistent-browser capture fallback for authorized restricted media.
- Xiaoe/XiaoeTong: persistent-browser capture of authorized HLS/media requests and subtitles.
- Local: inspect an existing local audio/video file.

Adapters resolve platform-specific media. A shared materializer handles download, stream extraction, remuxing, conversion, ffprobe validation, and atomic placement.

yt-dlp and ffmpeg remain implementation details:

- yt-dlp resolves and downloads supported streams.
- ffmpeg performs decoding, audio extraction, remuxing, conversion, TTS concatenation, frame extraction, and loudness normalization.

The system prefers independent audio streams. A playable video does not guarantee a separately addressable audio stream; muxed HLS may require transferring video-bearing segments even when only audio is retained. DRM remains unsupported.

## 7. AI pause protocol

`ensure` cannot invoke the current Codex model directly. When semantic work is required, it returns a structured pause:

```json
{
  "status": "awaiting_ai",
  "action": "podcast_translate | evidence_select | notes_write",
  "input": "package-relative input path",
  "output": "package-relative expected output path",
  "schema": "schema identifier",
  "resume": ["video-extract", "ensure", "..."]
}
```

The `video-learning` skill reads the input, writes the expected structured output, validates it through the CLI, and reruns the exact resume command. It must not bypass the validator.

Podcast translation is batched for long media:

- batches are at most 12 minutes or 8,000 source characters, whichever comes first;
- spoken segments are 30–90 seconds where natural boundaries permit;
- completed batch outputs are independently fingerprinted and reused;
- retries process only missing or invalid batches;
- the final localized script preserves source start/end timestamps for audit and repair.

## 8. TTS implementation

Define an internal `SpeechSynthesizer` port because speech synthesis is an external dependency. First production adapter:

- macOS `/usr/bin/say`;
- default voice `Tingting` (`zh_CN`), overridable by `--voice`;
- render one validated segment at a time to AIFF;
- ffmpeg converts/concatenates segments to AAC-LC M4A;
- apply EBU R128-style loudness normalization for consistent listening volume;
- preserve per-segment artifacts until the final podcast verifies, enabling resume.

Tests use a fake synthesizer that creates deterministic short audio fixtures. A future cloud/high-fidelity TTS adapter may be added behind the same port, but no paid provider is part of this implementation.

## 9. Manifest v4 and validation

Create `schemas/manifest-v4.schema.json` and set `SCHEMA_VERSION = 4`. Schema versions 1–3 remain readable and are not destructively migrated.

Manifest v4 records:

- requested goals and normalized parameters;
- artifacts, including `source_audio`, optional `source_video`, `transcript_srt`, translation batches, `localized_script`, `localized_audio`, candidates, approved evidence, notes input, and notes;
- stage observations with duration, fingerprint, parameters, cache hit, and sanitized result;
- provenance indicating native Chinese track versus synthesized Chinese podcast;
- retention policy and whether the source video pre-existed;
- sanitized errors and explicit AI pauses.

Readiness remains artifact-derived. Do not store a `ready` list as authoritative truth.

Validation rules:

- audio/video: ffprobe-readable, non-empty, positive duration;
- transcript: ordered non-empty timed cues and language metadata;
- localized script: ordered source timestamps, non-empty Chinese text, complete batch coverage, no missing segment IDs;
- localized audio: readable positive-duration audio, all rendered segments represented, no partial files, provenance recorded;
- candidates/evidence/notes: retain current schema-v3 semantic checks;
- `notes_zh`: valid Chinese notes, transcript evidence, approved visual evidence or an explicit no-useful-visuals reason, and resolvable local links;
- cleanup must occur only after goal verification and must never delete a pre-existing video.

## 10. Code changes

Add:

- `video_extract/goals.py`: public goals, policies, defaults, and normalized request types.
- `video_extract/planner.py`: pure artifact/capability dependency planning.
- `video_extract/orchestrator.py`: execute plans, cache stages, emit AI pauses, and resume.
- `video_extract/media.py`: normalized inventory and shared materialization.
- `video_extract/podcast.py`: translation-batch preparation, script validation, TTS rendering, concatenation, and provenance.
- `video_extract/tts.py`: `SpeechSynthesizer`, macOS `say` adapter, and fake-test adapter.
- `schemas/manifest-v4.schema.json`.
- `tests/` covering planner, validator, orchestration, adapters, TTS, migration, and CLI contracts.

Modify:

- `video_extract/cli.py`: add `plan`, `ensure`, and goal-aware `verify/status` while preserving existing commands.
- `video_extract/contracts.py`: schema v4 capabilities, artifacts, and compatibility mapping.
- `video_extract/validate.py`: independent artifact-derived goal validation; remove the assumption that every valid package must retain video.
- `download_youtube.py`: separate stream inventory/selection from materialization; allow source-language audio when no Chinese track exists.
- `bilibili_source.py`: support audio-only selection and normalized inventory.
- `convert_voice_to_article.py`: expose stream-to-audio materialization and reuse formal subtitle/ASR helpers without forcing a retained video.
- `prepare_bilibili_learning_package.py` and `prepare_learning_package.py`: accept planner-provided retained or ephemeral video paths.
- `docs/package-contract.md`, `README.md`, `CONTEXT.md`, and a new accepted ADR after approval.
- `/Users/syz/.agents/skills/video-learning/SKILL.md`: map natural language to the two goals, handle AI pauses, and require final goal verification.
- `/Users/syz/.agents/skills/video-learning-ask/SKILL.md`: produce only `podcast_zh` or `notes_zh` requests plus retention/scope preferences.

## 11. Test plan

### Unit and contract tests

- Goal parsing supports one or both goals and rejects unknown goals.
- Planner never adds screenshot stages for `podcast_zh` alone.
- Planner always adds visual evidence stages for `notes_zh`.
- Native Chinese audio bypasses translation and TTS.
- Missing Chinese audio uses source audio, formal subtitles or ASR, AI translation pause, and TTS.
- Formal subtitles bypass Whisper.
- Missing subtitles invoke Whisper.
- Existing local video is reused and never deleted.
- `keep_video=yes` retains a complete video.
- `keep_video=auto/no` removes only a newly acquired proxy after successful notes verification.
- Failed or paused work preserves proxy media and resumable parts.
- Audio-only packages validate without a video artifact.
- Notes remain valid after an ephemeral video is removed.
- Schema 1–3 packages remain readable.
- Signed URLs, cookies, authorization headers, and tokens never enter manifests or command output.
- Repeated identical runs are cache hits and do not repeat download, ASR, translation, TTS, or frame extraction.

### Local media fixtures

Generate deterministic short audio/video fixtures with ffmpeg during tests. Do not commit large binaries.

### Manual end-to-end acceptance

1. YouTube `MGxcosNuC8k`:
   - request `podcast_zh`;
   - reuse/download the native `zh-Hans` track;
   - produce valid `audio/podcast.zh-CN.m4a`;
   - do not generate screenshots or notes.
2. YouTube `ddq8JIMhz7c`:
   - request `podcast_zh`;
   - detect absence of native Chinese audio;
   - acquire source audio, prepare/complete translation batches, synthesize Mandarin audio, and verify the podcast.
3. One existing Bilibili package with formal subtitles:
   - request `notes_zh --keep-video no`;
   - reuse subtitles, acquire a 480p working proxy, generate/select frames and Chinese notes, then remove only the proxy.
4. One existing Xiaoe package without formal subtitles:
   - request `notes_zh --keep-video yes`;
   - reuse or obtain the video, run local ASR, generate/select frames and Chinese notes, and retain the video.

## 12. Required verification commands

The implementation is not complete until all commands succeed:

```bash
uv run python -m compileall video_extract *.py
uv run pytest -q
uv run video-extract doctor --json
uv run video-extract --help
uv run video-extract plan 'https://www.youtube.com/watch?v=MGxcosNuC8k' --goal podcast_zh --json
uv run video-extract status /path/to/acceptance-package --json
uv run video-extract verify /path/to/acceptance-package --goal podcast_zh --json
```

Manual network acceptance must report any login, CAPTCHA, DRM, unavailable media, or external-state failure separately from code/test failures.

## 13. Execution order and commit-sized checkpoints

1. Add tests and manifest-v4 contract for independent audio/video capabilities and backward compatibility.
2. Implement normalized media inventory plus YouTube audio-only/native-language behavior.
3. Implement the pure planner, `plan`, and goal-aware `status/verify`.
4. Implement `ensure`, AI pause/resume, and idempotent stage observations.
5. Implement podcast translation batches, macOS TTS, concatenation, and validation.
6. Adapt Bilibili, Xiaoe, and local acquisition to the normalized inventory/materializer.
7. Implement notes retention policies and safe proxy cleanup.
8. Update both skills and project documentation.
9. Run unit/contract tests and the four manual acceptance scenarios.

At each checkpoint, preserve unrelated existing files and historical packages. Do not delete or rewrite existing media during migration.

## 14. Explicit non-goals

- No lip-synchronized video dubbing.
- No voice cloning.
- No paid/cloud TTS provider in the first implementation.
- No user-facing control over internal capabilities or gates.
- No automatic collection summary changes.
- No deletion of historical package artifacts.
- No DRM bypass, cookie export, CAPTCHA automation, or access-control circumvention.

## 15. Approval record

The user approved this plan before implementation. Execution was delegated to `gpt-5.6-sol` with low reasoning, followed by main-agent review, compatibility repair, and acceptance verification.

## 16. Acceptance record

- `podcast_zh`: `ddq8JIMhz7c` completed from source audio through timed transcription, complete 2,604-segment Chinese localization, `Tingting` synthesis, concatenation, and loudness normalization. The final M4A is 7,012.5 seconds and passes goal verification without retaining a video.
- `notes_zh`: the Bilibili sample passes with six approved screenshots and complete Chinese notes.
- `notes_zh`: the Xiaoe-derived sample passes from the existing authorized local video with three approved screenshots and complete Chinese notes. Direct page acquisition remains subject to the user's Xiaoe browser authorization and was not used for this acceptance run.
- Recovery behavior: an existing but invalid notes file now returns to the `notes_write` AI gate instead of failing terminally; regression coverage is included.
