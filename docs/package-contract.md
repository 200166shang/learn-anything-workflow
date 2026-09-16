# Video-learning package contract

This document and the v4/v5 schemas are the authoritative deterministic package contract. `video_extract.validate` implements artifact-derived semantics. Schema 1–4 manifests remain readable; stored status labels are hints only.

## Public goals

- `podcast_zh` requires a positive-duration, ffprobe-readable `audio/podcast.zh-CN.m4a` plus provenance identifying a native Chinese track or complete synthesized script coverage. It does not require video, screenshots, or notes.
- `notes_zh` requires a timed transcript, Chinese notes with timestamp evidence, and resolvable approved visual evidence, or an explicit reason that no useful visuals exist. It does not require localized audio.

`plan` is read-only. Pass `--output EXISTING_PACKAGE` to include its safely resolved, validated reusable artifacts. `ensure` may return `awaiting_ai` with an action, package-relative input/output, schema, and exact resume command. Semantic output must be written to that expected path and the resume command rerun; final completion always requires `verify --goal`.

Formal subtitles are materialized to SRT; when absent, source audio is passed through the existing local faster-whisper implementation. Existing but invalid notes are never accepted by filename alone: `ensure` returns to the `notes_write` AI pause until artifact-derived goal validation succeeds. `audio_quality=standard` means 128 kbps AAC and `high` means 192 kbps AAC for normalized and synthesized podcast audio.

Audio selection is independent of extractor format order: exact original language wins, followed by the declared default/language preference and then a safe fallback. Within the selected language, the best independent stream for the requested quality is used. Native Chinese follows the same bitrate rule. Stream URLs, Referer, User-Agent, cookies, authorization headers, and signed parameters remain hidden; materialization passes them directly to ffmpeg without manifest or plan persistence.

Planning performs public metadata reads only. A failed Bilibili/Xiaoe metadata lookup yields an authorization-required unknown inventory; only `ensure` may open the existing persistent-browser capture fallback.

For schema v1–v3 packages, a safe, decodable `artifacts.audio` is promoted to `source_audio`. When `selected_audio_language` is `zh` or `zh-*`, suitable AAC/M4A is atomically reused as `localized_audio` and recorded as legacy native-Chinese provenance; non-Chinese legacy audio remains source-only.

`keep_video=auto|yes|no` is retention policy. Existing/local video is never deleted. A newly acquired proxy for `auto` or `no` is removed only after requested goals validate; failures and pauses retain media and resumable partials.

## State model

Collection states are `catalog_ready` and `summary_complete`. Item states are ordered independently: `media_ready`, `transcript_ready`, `candidates_ready`, `evidence_selected`, `notes_complete`.

- `catalog_ready`: parseable complete inventory; unique stable ID and order/chapter per item; inaccessible items retained; declared denominator equals inventory.
- `media_ready`: requested non-empty media is ffprobe-readable with positive duration; platform, stable identity, metadata, and status context exist.
- `transcript_ready`: media passes and the SRT parses with at least one timed, non-empty cue.
- `candidates_ready`: candidate JSON has unique IDs, non-negative timestamps, readable package-local images, and a readable contact sheet.
- `evidence_selected`: review JSON records `model_only` or `human` and `approved_by`; selected entries resolve to candidate ID/timestamp/image. Zero selected images requires `no_useful_visuals_reason`. `notes_input.md` exists.
- `notes_complete`: semantic learning sections and timestamp evidence exist; every numbered knowledge block is covered or explicitly excluded; approved images and all local links resolve; no signed URL is embedded.
- `summary_complete`: summary manifest freezes intended item IDs/order, every included item has verified notes, exclusions are explained, and links resolve.

## Manifest v4

Paths are package-relative and cannot escape the package. Writes use a temporary sibling file plus `os.replace`. A stage observation records `duration_ms`, `input_fingerprint`, parameter summary, `cache_hit`, and sanitized result. Errors and output omit cookies, authorization headers, tokens, and temporary signed media URLs.

Typical fields are `schema_version`, `platform`, `identity`, `title`, `request`, `retention`, `artifacts`, `stages`, `provenance`, and optional `pause`/sanitized `errors`. New artifacts include `source_audio`, optional `source_video`, `translation_batches`, `localized_script`, and `localized_audio`, alongside the schema-v3 evidence and notes artifacts.

## Recovery and concurrency

Identical validated input/parameter fingerprints are cache hits. Browser capture stays serial per persistent context. Downloads, ffmpeg, and Whisper have separate safe concurrency; Whisper models are reused within each worker. Downloads preserve resumable part files and only advance verified state after media validation. Candidate extraction batches timestamps in one ffmpeg process and starts from reduced-resolution images; high resolution is retained only when useful.

The stable interface adds `plan` and `ensure` while preserving `doctor|scan|acquire|transcribe|prepare-evidence|verify|status`; `--help` owns current defaults and platform parameters.

## Manifest v5 Chinese listening audio

For `--media audio --language zh-CN`, native Chinese audio completes directly. Without a native Chinese track, an English source yields `mandarin_audio.mode=external_pyvideotrans` and `input_artifact=media/audio.source.m4a`; ensure may pause normally as `awaiting_external`. A non-English or unknown source yields `unsupported` and no external command is run.

The top-level `extract-media` workflow extracts original audio and directly invokes pyVideoTrans podcast mode. pyVideoTrans independently owns `listening/zh-CN/` and its manifest, 48 kHz mono 64 kbps MP3, production report, private run state, and intermediates. video-extract does not copy, validate, or adopt those artifacts. This is a natural-paced Chinese listening edition whose duration may differ from the source; synchronized dubbing and remux-ready tracks are outside the contract.
