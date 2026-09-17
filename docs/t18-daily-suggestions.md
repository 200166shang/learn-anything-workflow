# T18 Daily suggestions and Codex choice

Issue: `#30`. Specification: `#12` (US31, US32; I2, I3, I7, I8; A11, A12).

## Public contract

- `video-extract suggestions today` reads the authoritative Learn, Card, and Review generations for one Asia/Shanghai date. It returns at most three stable suggestions with the exact object/version, reason, and a 10–15 minute estimate.
- Due selected cards rank first. Recent confused, prompted, or not-recalled questions rank next. A current or root question with a persisted explanation is a fallback. Parked questions, completed question reinforcement, stale card answers, future cards, and content without a persisted explanation are excluded.
- Suggestions are not saved. Choosing the proposed item or a different valid question/card proceeds through `review prepare` and `review record`; skipping writes nothing. Learn position, feedback, and return routes remain unchanged.
- Empty eligible input is a successful empty result. An unreadable or corrupt authority is an explicit failure. The command performs no model call, remote delivery, scheduling, or paid operation.
- Longer Practice is an explicit alternative rather than a default daily item and must state that it exceeds the short-session estimate.

## Verification

`tests/test_daily_suggestions.py` covers due-card priority, read-only calculation, completed/parked filtering, confused-question reinforcement, explicit read failure, and the stable capability entry.
