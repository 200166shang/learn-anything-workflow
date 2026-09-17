# T24: legacy pilot migration and rollback

Issue: `learn-anything-workflow#36`.

The public seam is `video-extract migration plan|convert|verify|cutover|rollback`.
It handles exactly one explicitly selected legacy course package and one legacy
learning-thread JSON file per named batch. The ordinary source and learning
runtimes continue to accept only workspace v2, source-package v6, and
learning-record v2; they do not discover or interpret the legacy input.

`plan` is read-only with respect to the legacy input and results store; it only
writes its resumable report under the workspace's local operational area. It records the exact input file digests, legacy schema and
source version, note/image/question/relation counts, current learning position,
and absent facts. Missing feedback or operation receipts stay explicitly absent.
`convert` copies into a private local batch stage and creates stable old-to-new
identity mappings without changing the v1 input. `verify` validates the target
learning schema and compares content, images, relationships, versions, and
position against the plan. Any source change invalidates cutover.

`cutover` requires an authorization JSON that names the exact batch, legacy
package, legacy thread, and `"approved": true`. It publishes the copied course, registers its source-package v6 version,
merges the converted learning-record v2 objects, and only then records the batch
as owned by the new store. The legacy package is registered read-only in the
ownership ledger and its original filesystem modes are saved before write bits
are removed from both the package and thread; there is no dual write. The mode
snapshot is persisted before the permission transition. Interrupted cutover is retryable when
the already-written target is byte-for-byte the verified conversion.

`rollback` does not restore a pre-cutover snapshot over current results. It
copies every new or changed migrated-course file, the current learning generation
when it advanced, and every operation receipt
written since cutover into `local/migration-preserved/<batch>/<timestamp>/`,
marks replay as required, and transfers ownership back to the legacy location.
It also restores the legacy package's recorded filesystem modes and records any
deleted migrated-course paths for reconciliation.
The new location remains read-only evidence until its preserved increments are
reviewed; uncertain remote operations are never replayed automatically.

Legacy question locators are verified against the copied Markdown and exposed
through `learning locate` as `legacy_migrated` locations. They are not promoted
to formal explanation revisions because doing so would fabricate the teaching
review and evidence fields that the legacy data did not contain.

The automated F-recovery fixture lives in `tests/test_pilot_migration.py`. It is
deliberately synthetic and proves only the deterministic contract. A real pilot
remains pending explicit user authorization; its private evidence must not be
posted to the public issue.
