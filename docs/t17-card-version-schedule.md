# T17 Selected cards, versions, and scheduling

Issue: `#29`. Specification: `#12` (US24, US28–US30; I2–I4, I7; A09, A10, A17).

## Public contract

- `video-extract card propose` returns a non-persisted candidate and reports whether the same normalized memory target and applicability conditions could reuse an existing card. `card select` is the only operation that saves the candidate or associates another question.
- `card revise --change-type wording` appends a formal wording revision to the active card version without changing its effective date or schedule. `--change-type material` preserves the old version and creates a new version whose first due date is the next Asia/Shanghai day.
- `review prepare --card-version-id` pins the exact card and explanation version without revealing the answer. If the bound explanation is no longer current, preparation pauses until the card is reviewed. `review record --review-date` stores the actual recall fact, optional correction, and returns the derived next schedule.
- `card schedule` is read-only. Rule version 1 derives next-day, 3/7/14/30-day, and maintained 30-day intervals from the selected version's effective date and scored Review facts. Prompted or not-recalled events reset to next-day; not-scored events do not affect the sequence. Dates use actual Asia/Shanghai review dates, not missed-day counts.

Cards are an immutable-generation authority under the results root and are included in result-only backups. Schedule caches are derivative: deleting them or running `workspace rebuild` does not alter card or Review facts, and `card schedule` reconstructs the same result.

## Automated evidence

`tests/test_card_workflow.py` exercises the public CLI for selection boundaries, same-target reuse, condition separation, both revision types, version-pinned Review, stable-event replay, the full interval sequence, reset behavior, and cache-independent reconstruction. Existing Review and backup suites verify that the new domain does not alter Learn state and survives isolated result restoration.
