# Issue 33 acceptance evidence

This record covers the automated protocol evidence for `publish.netease`. It does not claim a real NetEase upload.

- Engineering baseline: `42c695463eb51fdc17bacc0a44d7c8f85874395a`; implementation branch `codex/issue-33-netease-publish` (final commit recorded by Git history).
- Contract: `netease-publish-request-v1`, schema version 1; operation and sanitized receipt schema version 1.
- Input/source version: generated schema-v5 package `source-1`, authenticated fake `audio.mandarin` operation receipt, and pinned output SHA-256. No private learning data or credentials are used.
- Public operations: `capability run/check publish.netease`; `operation show/resume/reconcile`.
- Expected: missing authorization makes zero adapter calls; pagination precedes upload; ambiguous candidates and unknown status stay uncertain; interrupted submissions only reconcile; completion requires a matching visible remote object and durable sanitized receipt.
- Actual: `tests/test_netease_publish.py` exercises those cases through the public CLI with an independent fake adapter; the repository suite and built-wheel contents are verified before delivery.
- Verdict: **PASS — automated fake-adapter protocol only**.
- Real integration: **PENDING USER AUTHORIZATION** — login, account selection, upload, remote visibility, and user experience have not been tested. No real library query, upload, payment, login, global install, or credential access was performed for this ticket.

The real check must use a user-approved audio object and non-secret account/authorization references. Record only the operation ID, source identity, audio digest, sanitized filename, remote object ID, confirmation facts, expected/actual result, and pass/fail/pending verdict; never record cookies, API keys, tokens, private configuration, or account secrets.
