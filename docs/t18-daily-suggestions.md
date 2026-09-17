# T18 Daily suggestions and Codex choice

Issue: `#30`. Specification: `#12` (US31, US32; I2, I3, I7, I8; A11, A12).

## Public contract

- `video-extract suggestions today` reads the authoritative Learn, Card, and Review generations for one Asia/Shanghai date. It returns at most three stable suggestions with the exact object/version and reason; the whole default group is capped at 15 minutes.
- Due selected cards rank first. Recent confused, prompted, or not-recalled questions rank next. A current or root question with a persisted explanation is a fallback. Stable ordering and de-duplication use saved card memory targets/conditions plus saved module goal/scope, question title, unresolved confusions, and feedback. Parked questions, completed question reinforcement, stale card answers, future cards, and content without a persisted explanation are excluded.
- Suggestions are not saved. `--prefer-question-id` and `--prefer-card-version-id` put the user's explicit choice first without creating hidden state. Choosing the proposed item or a different valid question/card proceeds through `review prepare` and `review record`; skipping writes nothing. Learn position, feedback, and return routes remain unchanged.
- Empty eligible input is a successful empty result. An unreadable or corrupt authority is an explicit failure. The command performs no model call, remote delivery, scheduling, or paid operation.
- Longer Practice is an explicit alternative rather than a default daily item and must state that it exceeds the short-session estimate.

## Verification

`tests/test_daily_suggestions.py` covers due-card priority, read-only calculation, completed/parked filtering, confused-question reinforcement, explicit read failure, and the stable capability entry.

## Acceptance evidence

| Item | Evidence |
| --- | --- |
| Engineering and schemas | Tested implementation `4093b2280893b67cfd7ef9f9a0f7e8febbc45335`, based on `0586291b`; command-response v1, learning-record v2, card-record v1, review-record v1. |
| Input/source versions | Isolated fixtures register byte-derived `source-version-*` identities, then use the resulting committed explanation pins, selected card versions, and immutable Review facts. |
| Public operations | `suggestions today`, `review prepare`, `review record`, and `capability run learning.suggestions`. |
| Expected | Stable maximum-three group capped at 15 minutes; explicit preference first; completed, parked, future, and stale candidates excluded; empty and failed reads remain distinct. |
| Actual | 2026-09-17: `uv run pytest -q tests/test_daily_suggestions.py tests/test_card_workflow.py tests/test_review_workflow.py tests/test_learning_workflow.py tests/test_capabilities.py tests/test_cli_contract.py tests/test_installation.py` → `97 passed in 41.79s`; pointer hashes remain unchanged during calculation. |
| Disposition | T18 automated/public-contract slice passed. Real morning delivery remains pending in T22/T23/T26 and is not claimed here. |
