# ADR 0002: Goal-driven public workflow

**Status:** Accepted

## Decision

Expose exactly `podcast_zh` and `notes_zh` as public goals. Keep artifact gates internal for deterministic planning, caching, validation, and recovery. Treat `keep_video` as retention policy. Semantic translation, evidence selection, and note writing use explicit validated AI pauses because the CLI cannot invoke the calling model.

Schema v4 records normalized requests, provenance, retention, artifacts, observations, and pauses. Readiness is derived from artifacts. Schema 1–3 packages remain readable and are not migrated destructively.

## Consequences

Podcast-only packages can complete without video. Notes can remain valid after a newly acquired working proxy is removed. Existing/local videos are never cleanup targets. Platform adapters resolve authorized media inventory; shared code materializes it. DRM bypass, voice cloning, paid TTS, and lip-synchronized dubbing remain out of scope.
