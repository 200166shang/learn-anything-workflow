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
| Engineering and schemas | T18 branch based on `0586291b`; command-response v1, learning-record v2, card-record v1, review-record v1. |
| Input/source versions | Tests create a registered byte-derived source version, a committed explanation pin, selected card versions, and immutable Review facts. |
| Public operations | `suggestions today`, `review prepare`, `review record`, and `capability run learning.suggestions`. |
| Expected | Stable maximum-three group capped at 15 minutes; explicit preference first; completed, parked, future, and stale candidates excluded; empty and failed reads remain distinct. |
| Actual | Automated public-CLI tests exercise every branch above without changing the Learn/Card/Review pointers during suggestion calculation. |
| Disposition | Automated slice passed; real morning delivery remains pending in T22/T23/T26 and is not claimed here. |
