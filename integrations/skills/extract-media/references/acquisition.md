# Acquisition and recovery

Run `plan` before writes; it is read-only. Let `ensure` choose streams and keep yt-dlp partial files for finite retry/resume. Network and subprocess work must have bounded timeouts and sanitized failures. `video`, `audio`, and `subtitles` requests materialize only their requested artifacts. An unavailable subtitle is an explicit result, never a fabricated transcript.

Canonical packages are keyed by platform identity (or local content fingerprint), not title. Collections reference item identities through `catalog.json`; they do not copy item packages.
