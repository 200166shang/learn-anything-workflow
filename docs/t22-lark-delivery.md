# T22 one-way Lark delivery and recovery

Issue: `#34`. Specification: `#12` (US31, US32, US36, US37, US41; I2–I4, I8, I9; A11–A13).

## Public contract

- `delivery enable` requires a logical recipient, pinned adapter, explicit authorization reference, and explicit effective date. The adapter performs a fresh local credential/identity/recipient readiness check. The physical Lark conversation ID remains in `VIDEO_EXTRACT_LARK_CHAT_ID`; it may identify a group or an already resolved P2P chat, is fingerprinted, and is never written to Results or public output. Requiring a chat ID lets reconciliation constrain and verify the original conversation instead of trusting a marker copied elsewhere.
- `delivery tick` uses Asia/Shanghai time, sends nothing before 09:00, recomputes `suggestions today`, limits the message to three items, and is quiet for a successful empty set. An authority read failure writes a failure fact and is not reported as empty.
- The persistent dedupe identity is workspace + `daily_review` + Beijing date + logical recipient. Before the adapter is called, delivery-operation-v1 stores the authorization, adapter identity, content digest, related object IDs, idempotency marker, and query handle. The message is one-way and directs all choice and teaching back to Codex.
- A completed operation is never resent that day. A submitted-but-unconfirmed attempt stays `uncertain`; `delivery reconcile` queries the original marker and never treats no search result as proof of non-delivery. Only an explicit adapter `not_submitted` result becomes retryable.
- `delivery status`, `delivery reconcile`, and `delivery disable` expose state without physical recipient IDs or credentials. A changed adapter, authorization, local credential, or recipient fingerprint requires fresh user authorization instead of silently redirecting an existing intent.

The production adapter invokes `lark-cli im +messages-send` as the authorized user with a stable idempotency key and reconciles through the public message-search command. It does not implement replies, cards, teaching, or background scheduling. T23 owns the unique 09:00 host.

## Acceptance evidence

| Field | Evidence |
|---|---|
| Engine / schema | Integration branch after #26; `delivery-operation-v1`; `command-response-v1`. |
| Input / source | Synthetic schema-v2 workspaces with authoritative Learn/Card/Review generations; no production recipient or credential. |
| Public operations | `delivery enable/disable/tick/status/reconcile`; `suggestions today`. |
| Expected | Pre-09:00 and empty are quiet; intent precedes transport; same-day duplicates send once; submitted interruption never resends; explicit available/not-submitted/unknown results remain distinct; corrupt recommendation authority records failure. |
| Actual | `tests/test_delivery.py` uses a countable independent adapter and exercises the complete manual path without external transmission. |
| Disposition | Automated/manual-path contract passed. Real recipient readiness and first receipt are **pending explicit recipient + content + identity authorization** and are not claimed by this ticket evidence yet. |
