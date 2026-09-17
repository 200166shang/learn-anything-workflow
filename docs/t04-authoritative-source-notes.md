# T04 authoritative source notes

Workspace-v2 source notes use a store under `results/source-notes`. The source
snapshot remains the sole authority for source facts and bytes. A note records
`workspace_id` context plus `source_id` and `source_version` foreign keys; it
does not copy source text into the note store.

`notes prepare SOURCE_ID` reads the pinned registered document and writes a
model input under the machine-local role. Plain text receives heading or
`paragraph:N` locators. SRT retains only parsed, complete cue ranges and is
explicitly marked `unverified_video_association` unless `source associate`
verifies structured evidence. Verified digest evidence pins both source IDs,
both source versions and both matching SHA-256 facts; arbitrary text remains
`unverified` and cannot establish sameness.
The command returns `awaiting_model`, never `awaiting_user` for model work.

`notes finalize SOURCE_ID --request REQUEST.json` validates the note body,
source locators, corrections, association statement and adopted attachments.
It stores immutable objects, writes a complete `notes-snapshot-v1` commit, then
atomically replaces one current pointer. Every historical revision stays in
the commit history. Every revision directly pins its body, citations,
corrections, association and adopted attachments, so it can be reconstructed;
the commit object set must be exactly reachable from these records. An `expected_revision`
conflict preserves the submitted request in `candidates/` without choosing a
winner. Identical valid content is reused.

The source snapshot and notes snapshot are intentionally separate consistency
boundaries. Notes pin a source version already committed by T03; a note commit
cannot make a source version visible. `notes audit` enumerates every note
commit and deeply verifies one complete unbranched chain back from current,
workspace identity, all note objects and source foreign keys, providing
the common rebuild seam for later projections. `notes reconcile` repeats that
validation and fsyncs every object, shard directory, commit, commit directory,
pointer and store directory. A failed barrier leaves durability unconfirmed.
If publication stops after an immutable commit is durable but before its
pointer is published, the recoverable response names that exact commit and an
executable `notes reconcile --commit-id ...` action. Reconcile publishes it
only when it validates and is the unique next revision after current; an
unclaimed orphan still makes ordinary audit fail.

No command in this slice creates a learning question graph, invokes a model,
produces audio, uploads content, or modifies the original transcript.
