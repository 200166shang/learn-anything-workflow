# Issue 39 — A01–A18 first-release acceptance

Date: 2026-09-17 (Asia/Shanghai). Specification: GitHub issue #12. Carrier:
GitHub issue #39. Engineering baseline before this closeout: `c8795a0`.

This record distinguishes deterministic automation, real local integration, and
explicit user acceptance. It does not relabel a one-minute scheduler observation
as a real next-day event. The user explicitly accepted that shorter observation
in #38 and instructed the workflow to pass without waiting overnight.

## Evidence matrix

| Scenario | Disposition | Evidence |
| --- | --- | --- |
| A01 scope and reuse | Passed | `test_scoped_media_operations.py`, `test_source_notes_workflow.py`, and the authorized six-thread Xiaomo migration preserve source versions and reuse completed operations. |
| A02 evidence and source change | Passed | `test_source_change.py`, `test_authoritative_notes.py`, and `test_learning_workflow.py` cover changed/missing sources, locator boundaries, corrections, and affected-state propagation. |
| A03 identity and follow-up | Passed | `test_learning_workflow.py` covers stable question identity, multiple entry relationships, return routes, read-only browsing, and independent feedback. The migrated pilot contains 26 questions and 20 relationships. |
| A04 three teaching modes | User accepted | The three profile contracts and real source alignment are covered by `test_learning_workflow.py`; #38 carries the user's acceptance. This record does not claim a new live oral explanation was performed during T27. |
| A05 merge and correction recovery | Passed | Explanation commit/restore, stable sections, cross-root reuse, and correction preservation are covered by `test_learning_workflow.py` and `test_cross_root_reuse.py`. |
| A06 actual navigation | Passed | Real Obsidian carrier evidence is content-addressed in `docs/evidence/t14-obsidian-ui.json`; all six migrated generations were rebuilt. `test_learning_view.py`, `test_navigation_overview.py`, and `test_issue25_acceptance_vault.py` cover rebuild and failure boundaries. |
| A07 continue later | Accepted substitution | Resume/back and persisted routes are deterministic in `test_learning_workflow.py`; the user explicitly waived the overnight wait in #38. No claim of an actual next-day session is made. |
| A08 small practice | User accepted | `test_practice_workflow.py` covers isolated user code, hints, attempts, normal/expired/boundary cases, stable checkpoints, conflicts, and unchanged Learn state. #38 records user acceptance; T27 did not invent a new handwritten attempt. |
| A09 card lifecycle | User accepted | `test_card_workflow.py` covers selection-only persistence, target reuse, wording/material revisions, version history, and result-only restore. |
| A10 schedule boundary | Passed | `test_card_workflow.py` verifies next-day then 3/7/14/30-day progression, reset after prompting, replay idempotency, late-date calculation, and cache-free rebuild. |
| A11 suggestion and choice | Accepted substitution | A real Lark send produced a recipient-bound receipt and same-day dedupe. The one-minute natural LaunchAgent run was accepted by the user instead of an overnight wait; it is not described as a real next-day 09:00 event. |
| A12 scheduler boundary | Passed | `test_delivery.py` and `test_delivery_schedule.py` cover before/after 09:00, empty/read-failure, same-day dedupe, no yesterday catch-up, public-CLI-only launchd, and unique-host state. The real LaunchAgent advanced one run and exited 0. |
| A13 uncertain external result | Passed protocol | `test_scoped_media_operations.py`, `test_delivery.py`, and `test_netease_publish.py` cover available/not-submitted/unknown recovery and no blind resubmission. No unrequested paid operation or upload was performed. |
| A14 entry and adapter replacement | Passed | `test_capabilities.py`, `test_installation.py`, and installed-skill checks cover stable capability IDs, dependency drift, replacement, and legacy-entry retirement. |
| A15 task and content concurrency | Passed | Learning, notes, practice, media-operation, and backup tests cover shared operation identity separately from expected-revision conflicts, candidate preservation, and uncertain outcomes. |
| A16 consistent commit and backup | Passed | Candidate/manifest/pointer interruption tests and backup copy revalidation prove readers see only complete generations. Real Obsidian generation evidence remains linked under A06. |
| A17 move and result-only restore | Passed | On the real learning Results, backup create, verify, isolated restore, and rebuild dry-run completed. All seven authority slots and the delivery receipt were represented; restored delivery/external operations were paused and unique-host verification was false. Missing external sources remained explicit. |
| A18 staged migration and rollback | Passed | `test_pilot_migration.py`, `test_migration.py`, `test_cutover.py`, and the authorized `04 → 02 → 03 → 01 → 05 → 00` cutover preserve legacy read-only copies, quarantine failed pre-conversions, retain new receipts, and retire daily legacy rules without deleting old data. |

## Real result-only recovery

The production Results were read while creating an isolated backup under the
workspace's excluded `local` role. The first attempt safely failed before
publication because the backup validator recognized only the older media
receipt shape, not a valid `delivery-operation-v1` projection. T27 added schema
validation for completed delivery receipts and a regression test that rejects a
damaged projection.

The repeated run completed create, deep verify, and restore. The manifest
declared all seven authority stores (`sources`, `notes`, `learning`, `review`,
`practice`, `cards`, and `operation_receipts`); empty domains remained explicit
rather than fabricated. The restored recovery state was `delivery=paused`,
`external_operations=paused`, and `unique_host_verified=false`. One real
delivery receipt survived. No production authority, source, or local scheduler
state was changed by the restore.

## Release decision and limits

The complete automated suite is the release gate, supplemented by the real
Obsidian evidence, real migration, real Lark receipt, real macOS scheduler, and
real result-only recovery. User-experience cases explicitly accepted in #38 are
identified as such above; the historical overnight requirement was waived, not
silently simulated. Long-term retention remains natural observation rather than
a release gate. Paid Mandarin generation and NetEase upload were not performed;
their recovery contracts passed with controlled adapters, as permitted by #12.
