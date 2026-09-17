# T09 explanation refactor and correction recovery

`explanation commit` remains the single publication boundary for a root
explanation.  Its existing contract is backward compatible; three optional
JSON inputs make a same-root refactor explicit:

- `--section-map` maps every affected historical question ID to one or more
  section markers in the candidate Markdown. Every marker must be mapped and
  every mapped question must belong to the selected root.
- `--revision-metadata` records a short change summary and the exact affected
  question IDs.
- `--corrections` records confirmed original claims, corrected claims,
  source-grounded evidence, and affected conclusions. Confirmed corrections
  live on the explanation, independently of an individual prose revision.

The publication updates Markdown, locators, revision history, and correction
facts in one learning snapshot. A stale `--expected-revision` preserves the
request as a conflict candidate without changing the current snapshot.

`explanation restore --question-id ID --revision N` creates a new revision. It
uses the selected revision's expression and section map, then overlays every
confirmed correction before publication. It does not replace the learning
record with an old snapshot, so thread position, original questions,
understanding feedback, review facts, and practice facts are not rolled back.
If a correction cannot be safely overlaid or a historical locator is missing,
restore fails without publishing.

Learning-record v2 readers continue to accept #19 revisions that predate T09.
New revisions add typed `revision_kind`, change summary, affected questions,
introduced correction IDs, and (for restores) the source revision. The stable
`learning.learn` capability remains contract version 1 and forwards the new
optional inputs and `explanation.restore` action.

Automated fixtures verify the deterministic storage and recovery contract.
Real teaching value and cross-root reference maintenance remain joint
acceptance with T26 and T12 as required by specification #12.
