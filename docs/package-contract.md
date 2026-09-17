# Video-learning package contract

> Current path convention: discover `workspace.toml` through `WorkspaceConfig`. Package artifact paths inside manifests remain package-relative. Media is authoritative; Obsidian export, SQLite, and playback are rebuildable derivatives. Machine-specific absolute paths may appear only as convenience fields in generated output and must never be used as identity keys.

This document and the v4/v5 schemas are the authoritative deterministic package contract. `video_extract.validate` implements artifact-derived semantics. Schema 1–4 manifests remain readable; stored status labels are hints only.

## Scoped media operations (request v1)

`plan --request`, `ensure --request`, and `capability run media.acquire`
accept `media-acquire-request-v1.schema.json`. The request pins a registered
`source_id`/`source_version`, an explicit sorted set of inventory item IDs, and
the requested media kinds, language, and quality. It never expands the scope or
turns a missing language track into localization work.

The stable operation key includes the logical workspace ID, source identity and
version, explicit scope, effective parameters, and capability contract version.
Operation state lives below the workspace's local role; artifacts live below
the results role and are reusable only while their recorded size and SHA-256
facts still match. `operation show` is read-only and `operation resume`
rechecks those facts before filling only missing or invalid artifacts.

Every media adapter implements acquisition plus reconciliation by stable
idempotency token/query handle. Reconciliation reports `not_submitted`,
`retry_safe`, `committed`, `available`, or `unsupported`; only the first two
permit resubmission. `committed` remains uncertain until the existing result is
queryable and locally verified. The operation store separately maintains a
monotonic fencing counter and lease owner. Adapter results are accepted only
while both still match the persisted lease, so an expired worker cannot publish
over a newer attempt.

A reconciliation transport failure is itself a persisted query attempt. It
keeps the original idempotency token and query handle unchanged, remains
`uncertain`, and offers only another `operation reconcile` command; it never
falls through to acquisition resume.

Before invoking an adapter, the operation store durably records an intent that
pins the capability/contract version, source version, effective parameters,
adapter identity, authorization category, and (when supplied) a non-sensitive
authorization reference. Adapters that declare authorization as required stop
at `awaiting_user` until that reference is present; credentials are never an
accepted request field. Resume and reconcile reject adapter identity drift, so
replacing an implementation cannot silently send an old uncertain request to a
different provider. Completion writes a sanitized operation receipt below the
results role so future result-only snapshots and backups retain reconciliation
evidence without copying leases, process details, or diagnostics. The local
operation record is committed first with a monotonic authoritative revision and
digest; the results receipt is a rebuildable projection carrying that pair.
An interruption between those writes can therefore omit a receipt but cannot
create a results-only success that lacks an authoritative completion. A later
execution of the same capability validates the authoritative digest and repairs
the projection; read-only `operation show` does not write it.
Receipts for `not_submitted`, `committed` (unknown), and `available` preserve
all sanitized attempt/query handles and structured remote receipt identifiers,
ledger digests, and fact summaries; free-form provider evidence is excluded.

## Current public workflows and retained validation goals

- New acquisition uses `plan/ensure --media`; it never synthesizes a missing language track.
- New source-note work uses `source import`, `notes prepare`, and `notes finalize`.
- `podcast_zh` and `notes_zh` remain artifact validation names for historical packages and final note checks; they are not public `plan/ensure --goal` inputs.

`plan` is read-only. Pass `--output EXISTING_PACKAGE` only for an explicitly selected package. `notes prepare` returns `awaiting_ai` with package-relative input/output and a resume argv; `notes finalize` verifies, exports, and updates the library.

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

## Result-only backup generation v1

`backup create TARGET` publishes a new directory only after deeply validating and
pinning one complete source, authoritative-notes, and learning generation.  Its
`backup-commit-*` manifest records the engineering revision, every authority's
schema/commit/revision/root digest, and the size and SHA-256 of every copied
file.  Current manifests contain their complete formal version history; every
object reachable from those manifests and every sanitized result-side operation
receipt is included.

Uninitialized source/notes/learning authorities are recorded explicitly as null
commit/schema, revision zero and an empty root digest. They do not require a
synthetic publication; an entirely empty workspace has zero entries. Missing or
invalid pointers beside existing commits are rejected instead of treated as empty.

The backup is intentionally result-only.  Source media and referenced source
trees, local operation state, leases and locks, candidates/drafts, derived
indexes and views, temporary files, installation state, and credentials are not
copied.  A referenced source which is absent after restore remains identified by
its stable source ID/version and is reported as `missing_external_source`; it is
never substituted with a different version.

`backup verify` rejects missing, extra, symbolic, digest-mismatched, or
association-broken payloads. `backup restore` accepts an explicit workspace-v2
configuration whose results root does not yet exist, validates in a sibling
staging directory, and atomically publishes the complete results root. The copied
exact file set and every digest are checked against the original pinned backup
manifest before publication, including operation receipts. Restore does not
merge with or overwrite an existing target. Restore writes only a recovery
state which pauses delivery and external operations until the user verifies a
unique host; rebuildable indexes and views remain the responsibility of their
registered capabilities.

## Manifest v5 media language requests

For `--media audio --language zh-CN`, an existing Chinese track is materialized as source audio. Without that track, extraction records `missing_requested_language_track`; it never starts synthesis.

The independent `mandarin-audio` skill consumes a managed package. It owns native-track normalization or the configured pyVideoTrans podcast call and produces `listening/zh-CN/podcast.zh-CN.mp3`. This is a natural-paced Chinese listening edition whose duration may differ from the source.
