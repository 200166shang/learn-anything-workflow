# T10: consistent result backup and isolated restore

Issue: `learn-anything-workflow#22`.

The public seams are `backup create`, `backup verify`, and `backup restore`.
Backup manifest v1 is a result-only generation: it pins the complete source,
source-note, and learning authority generations plus sanitized operation
receipts.  Each authority records its commit, revision, schema, and root digest;
the backup commit signs the full manifest.  Copying candidates, local operation
state, locks, credentials, temporary files, source media, or rebuildable views
is forbidden.

Creation validates each pinned manifest and object before copying, confirms the
authority pointers did not change, deeply validates the staged payload, then
publishes the backup directory with one rename.  Verification checks its schema,
commit identity, exact file set, every file digest, all authority root digests,
store-specific deep invariants, and cross-store source-version references.

Restore never overlays a results root.  It copies to an isolated sibling stage,
re-validates all authority stores and receipts, writes a recovery state with
delivery and external operations paused, and then publishes the results root
with one rename.  Location-only sources are explicitly reported missing; notes,
learning records, explanations, attachments, history, and receipts remain
readable from the restored results alone.  Reattaching a source still requires
the existing source-version verification contract, so a different source
version cannot replace old evidence.

This slice proves automatic recovery for the source, authoritative-note,
learning, and operation-receipt types currently implemented. Practice, cards,
delivery ledgers, real Obsidian generation reads, and the full A17 exercise stay
with their owning later tickets and T27 acceptance.
