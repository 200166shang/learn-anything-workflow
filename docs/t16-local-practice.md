# T16 local practice contract

Issue: `learn-anything-workflow#28`.

The public seams are `practice prepare/checkpoint/record` and the stable
`learning.practice` capability. Practice v1 is an independent fact store; it
does not write learning-record-v2, move a Learn position, or infer an
understanding state from test results.

`prepare` accepts exactly one small mechanism with a bounded effort estimate,
completion criteria, and a simulation disclaimer. It creates separate
`example/`, editable `task/`, `tests/`, and `reference/` directories. Replaying
the same preparation is idempotent and never rewrites those files; reusing an
identity for different content is rejected. The recorded argv is descriptive:
the tool does not execute user or fixture code.

`checkpoint` independently enumerates editable paths twice without following
links, opens each path relative to no-follow directory descriptors, and reads
each regular file twice with stable stat metadata. It publishes a content
object only when both path/type sets, metadata, and contents agree. Symlinks are rejected. A stale revision
preserves a conflict candidate, while continuous edits return a recoverable
failure without advancing the authoritative pointer. Checkpoint objects,
practice facts, immutable manifests, and the current pointer are exposed by
`backup_entries`; caches are not part of that set.

`record` validates a closed, kind-specific event schema and stores concise hint
level, key-attempt, externally observed test, and reported-outcome facts.
Authoritative completion is derived rather than accepted from an outcome: it
requires a saved checkpoint, an attempt, and a passing `tool_observed` test;
any hint forces `with_hint`. A `reported_not_executed` test never completes a
practice, and fixture success never proves hardware timing or reliability.

The automated fixture covers normal, expired, and exact-boundary recognition
ages using a simulated clock. No hardware, private learning material, remote
service, dependency installation, or untrusted-code execution is involved.
