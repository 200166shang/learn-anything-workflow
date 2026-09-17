# T01 engineering preservation and installation evidence

Issue: `learn-anything-workflow#13`. This file records engineering facts only; no learning materials, media, credentials, browser state, or private source are included.

## Preserved baseline

- Original core history was retained: `e03dec8` → `77e576b`.
- The pre-T01 working tree was not represented by HEAD alone. It contained 19 tracked modifications and 11 untracked engineering files.
- Those changes were checked for ignored credentials/private state and preserved without rewriting history in commit `c2b52e0` (`Preserve pre-T01 core worktree`).
- Ignored `.lark-vc-auth.png`, `.venv`, caches, the outer VideoLearning workspace, media, and the Obsidian learning vault were not added.
- Before T01, the installed `video-extract` was an editable uv tool sourced from this core, while `extract-media`, `source-notes`, and `mandarin-audio` existed as standalone installation-directory copies.

## Ownership and transition

- Python tools, schemas, tests, project Agents, and project Skills are now maintained in this repository.
- Project Skills live under `integrations/skills/`; project Agents live under `.codex/agents/`.
- `video-extract install plan` is read-only. `apply` preserves every pre-existing non-linked target before linking host entries to the unique source. `check` reports revision, content fingerprint, dependencies, exact maintenance paths, and source/install drift.
- ADR 0001's evidence rules remain. ADR 0002's two-goal and permanent old-schema clauses await replacement. ADR 0003 and ADR 0004 remain superseded. T01 performs no learning-data migration or deletion.

## Acceptance record template

For each run, retain locally: engineering revision and contract version; sanitized input/source version; public operation; expected result; actual JSON/exit status; and pass/fail/pending. Do not publish sensitive input, media, private source, credentials, or user learning data.

## T01 verification summary

| Public operation | Sanitized input/source | Expected | Actual | Result |
| --- | --- | --- | --- | --- |
| `install plan/apply/check` | engineering revision `11e9f227894284a43347fd3ee4ae2424181a4685`; install contract 1; integration fingerprint `614d8e7b2e2ece170666fca822ca25acb56c1f894e78d4e766a21963ce8224bf` | preserve standalone copies, link five host entries, identify actual tool source and dependencies | exits `0/0/0`; backup receipt at the local path returned by apply; five entries `linked`; editable tool source matched; ffmpeg, ffprobe, yt-dlp, Pillow, and Playwright available | pass |
| `workspace show` | workspace schema 1 configuration; read-only real workspace | resolve the configured project, media, and results roots without modifying them | exit `0`; configuration resolved successfully | pass |
| `source import` → `notes prepare` → model candidate → `notes finalize` | temporary Markdown fixture SHA-256 `2dd37686153aff589d4707af16b6a2cafbd67ed9564c4380f96b525a8cabb398`; package source version recorded by import manifest | reach the model boundary, accept the candidate, validate and export the note | exits `0/0/0/0`; `awaiting_ai` → `ready` → `complete`; validation and exported file succeeded | pass |
| `workspace doctor` | workspace schema 1 configuration; read-only real workspace | expose current workspace health without treating old data as migrated | exit `1`; no migration regressions; library/playback valid; three pre-existing broken generated-image references keep overall status false | pending existing data repair; not modified by T01 |

The isolated checks do not claim A14/A18 in full, migrate a real old course/thread, or validate later learning-system contracts. Those remain for their owning tickets and T27 closure.
