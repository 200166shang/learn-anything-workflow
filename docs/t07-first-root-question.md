# T07: learn the first root question

Issue: `learn-anything-workflow#19`.

The public seams are `learning module create/show`, `learning recommend`, `learning thread create/show`, `learning pursue/locate`, and `explanation prepare/commit`. `learning.learn` is the stable capability ID with `learning-request-v1`; the shared data-driven capability checker validates it alongside `source.notes` and `media.acquire`.

Learning facts use an independent `learning-snapshot-v2` store below the workspace-v2 results role. It has immutable Markdown objects, complete manifests, a single atomic current pointer, revision conflict candidates, and a future backup-enumeration boundary. It references source-v6 identities and versions but does not modify source snapshot-v1.

Root recommendations are model pauses and are not persisted. Creating a thread records the selected root with opaque UUIDv4 module/thread/question identities before any explanation exists. `pursue` likewise records the original follow-up before explanation. Thread ownership is singular and module/thread current position is updated in the same learning commit.

`explanation prepare` returns registered source context, one stable section marker, evidence categories, and profile-specific teaching requirements. `commit` validates the marker, source versions, explicit semantic review facts, and executable profile checks before publishing a revision. Failed validation leaves the prior revision and locator current. Formal Markdown revisions remain immutable and locators name explanation, revision, and section identity rather than a heading slug.

The three first-slice profiles cover linear transformations, recognition-to-action chains, and one-frame processing pipelines. They are semantic guardrails, not a required chapter template. Real teaching quality remains pending the T26 user acceptance.
