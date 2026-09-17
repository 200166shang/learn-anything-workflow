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

`checkpoint` reads every editable task file twice and publishes a content
object only when both reads agree. Symlinks are rejected. A stale revision
preserves a conflict candidate, while continuous edits return a recoverable
failure without advancing the authoritative pointer. Checkpoint objects,
practice facts, immutable manifests, and the current pointer are exposed by
`backup_entries`; caches are not part of that set.

`record` stores concise hint level, key-attempt, externally observed test, and
outcome facts. Completion distinguishes `independent`, `with_hint`,
`example_only`, and `incomplete`; it never treats passing fixture tests as
hardware timing or reliability evidence.

The automated fixture covers normal, expired, and exact-boundary recognition
ages using a simulated clock. No hardware, private learning material, remote
service, dependency installation, or untrusted-code execution is involved.
