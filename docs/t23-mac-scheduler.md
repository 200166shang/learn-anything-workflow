# T23 macOS daily scheduler

The user-level launchd job invokes only `video-extract delivery tick --workspace … --json`.
It runs at load and once per minute; `delivery tick` alone owns Asia/Shanghai date, 09:00,
suggestion, deduplication, and recovery rules. Consequently sleep and shutdown do not promise
an exact 09:00 send: a same-day wake after 09:00 recomputes and delivers at most once, while a
later date never catches up yesterday.

Install only after `delivery enable` has verified the real adapter and the user has authorized
the target:

```console
video-extract delivery schedule-install --workspace /absolute/path/workspace.toml --json
video-extract delivery schedule-status --workspace /absolute/path/workspace.toml --json
```

The physical Lark chat ID is read from `VIDEO_EXTRACT_LARK_CHAT_ID` and stored only in the local
LaunchAgent plist. Public output and Results do not contain it. The active-host marker lives under
the workspace's `local/delivery` role, which portable backups exclude. A restored workspace is
therefore paused until the target, delivery ledger, and unique host are verified and explicitly
installed again.

Before moving the workspace to another Mac, stop the old host first:

```console
video-extract delivery schedule-remove --workspace /absolute/path/workspace.toml --json
video-extract delivery disable --logical-target LOGICAL_TARGET --workspace /absolute/path/workspace.toml --json
```

The scheduler does not require a persistent Codex task. Real next-day 09:00 delivery and the
return-to-Codex choice remain end-to-end acceptance work rather than simulated-clock evidence.
