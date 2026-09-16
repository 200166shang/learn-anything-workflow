# Separate media extraction from evidence-backed learning

**Status: accepted; supersedes ADR 0002 for new public workflows**

New work exposes `extract-media` for video, audio, subtitles, and standard media packages, while `video-learning` produces only Chinese evidence-backed notes. Both resolve a source to one canonical schema-v5 media package; audio localization is an internal, resumable project capability rather than a public Skill or a Skill-to-Skill call. This keeps large durable media in the canonical library and treats Obsidian as an export target for notes and adopted images only, while schema v1–v4 packages and the `podcast_zh` alias remain non-destructively compatible.

## Considered options

- Keep one public workflow for listening audio and notes: fewer entry points, but conflates distinct intent, storage, and verification rules.
- Implement localization in Skill instructions: easy to prototype, but not testable, resumable, or authoritative.
- Migrate old packages automatically: creates a tidier tree, but risks moving large user-owned artifacts during ordinary work.
