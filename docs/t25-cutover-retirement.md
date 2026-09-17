# T25: batch cutover and legacy runtime retirement

Issue: `learn-anything-workflow#37`.

## Runtime contract

`migration plan|convert|verify|cutover|rollback` is the only public legacy-data
boundary. A named batch records exact package, thread, question-document and
image digests; reports absent and unknown facts; and reconciles notes/images,
questions/explanations, practice code, cards/review history, and operation
receipts separately. Unsupported legacy values are retained byte-for-byte in
`opaque-legacy` with `never_automatic` replay policy. They are not interpreted
as current learning facts.

Cutover requires an exact authorization document. It publishes the verified
copy before assigning the batch to the new store, then makes the legacy package
and thread read-only. Batch ownership rejects overlapping legacy inputs. A
rollback freezes only that batch's migrated course, preserves its changed
course files and linked learning/domain stores, records deleted paths and
receipt deltas, and never invokes a remote adapter. Shared receipt and learning
roots remain writable for unrelated batches.

Normal runtime no longer exposes `migrate-legacy`, `scan`, `acquire`,
`transcribe`, or `prepare-evidence`; compatibility invocations return a
structured replacement without executing an old script. Direct `status` or
`verify` of schema 1–4 packages is rejected with `migration plan`. Recursive
discovery prunes migration staging, preserved rollback evidence, quarantine,
backup, and read-only legacy roots.

## Installation contract

The project is the only editable source for the current Learn, Review,
Practice, source-notes, extract-media and mandarin-audio Skills, both business
Agents, and the Obsidian navigation plugin. `install apply` links these entries,
records the engineering revision/content fingerprint, and moves known old Skill
entries out of host discovery into its timestamped backup. It creates no old
name forwarding aliases and deletes no old copy. `install check` fails if a
retired entry is reintroduced or a configured plugin/Skill/Agent drifts.

The Codex conversation selects Learn, Review, Practice, source preparation, or
audio by the user's intent. Stable capability IDs and public CLI contracts are
the programmatic seam; users do not need to remember installation paths.

## Automated evidence

The focused acceptance fixtures cover:

- two cut-over batches where an interrupted rollback of A leaves B writable;
- exact opaque preservation of unknown fields, cards, review history, practice
  facts, and uncertain historical receipts;
- rollback preservation of new course files, linked Learn state and receipts
  with no replay;
- transactional install rollback, preservation of hand-edited old Skills,
  old-entry reintroduction detection, and required Obsidian plugin publication;
- hidden/rejected legacy commands and exclusion of isolated roots from normal
  discovery.

These fixtures prove deterministic contracts only. The acceptance record for
the actual local cutover records private paths and counts locally; the public
Issue receives only non-sensitive totals, versions, commands, expected/actual
outcomes, and verdicts. Old copies remain present until the user separately
authorizes deletion.
