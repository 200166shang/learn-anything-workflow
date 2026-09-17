# T03 portable workspace and versioned source evidence

Issue: `learn-anything-workflow#15`.

Workspace schema v2 separates five location roles: engineering `project`, authoritative learning `results`, original `sources`, rebuildable `derived`, and machine-local `local`. Relative roots move with `workspace.toml`; absolute roots are explicit external mappings. Writes are limited to declared results/local roots after symlink resolution.

`source register` and workspace-v2 `source import` assign an opaque source ID independently of title, path, and content. A changed document or source tree creates a new source version under the same source. Source trees remain in place; their version basis includes a full file manifest, content digest, Git commit when available, and dirty working-tree state. Documents are retained as immutable content objects so a results-only copy remains readable.

Formal records use `schemas/workspace-v2.schema.json`, `schemas/source-package-v6.schema.json`, and `schemas/snapshot-v1.schema.json`. A write stores immutable objects, then a complete commit manifest, and finally replaces the single current pointer. Readers pin that commit and validate all referenced object digests. `expected_revision` conflicts preserve a candidate instead of overwriting the published version. Test-only fault injection proves that interruption immediately before or after pointer publication exposes either complete old state or complete new state.

Pointer visibility is not itself a durability claim. Object and manifest files are flushed, their containing directories are synchronized, and the results directory is synchronized after the current pointer replacement. If that final barrier fails, the response reports logically visible but durability-unknown state and returns an executable `source reconcile` command; reconcile revalidates the pinned manifest and objects and repeats the directory durability barriers before confirming completion. Every document version object must be reachable through `snapshot.objects`, whose digest and size record must match the actual immutable file.

Location changes are machine-local mappings. `source relocate` compares actual content with the registered source version before updating the mapping; a different version is reported and never substituted. Workspace v1 is not guessed or converted by source-v6 runtime commands and receives an explicit migration next action.

All implementation and tests use temporary directories. T03 performs no migration of a real learning library, no external upload, no paid operation, and no reminder enablement.
