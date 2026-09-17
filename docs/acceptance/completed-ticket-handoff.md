# Completed implementation ticket handoff

This record consolidates existing implementation branches for specification #12.
It does not declare the complete specification accepted. PR #40 remains draft.

## Baseline and provenance

The prior integration baseline is `e76327b96c1d5b539df7723d2a9264fa45950aef`.
The original worktrees and their branches are retained. Integration takes place
in `codex/completed-ticket-closeout`, followed by a fast-forward of
`codex/issue-12-learning-system`. No history is rewritten.

| Ticket | Verified implementation head before closeout | Disposition |
| --- | --- | --- |
| #13 | `9648125` | Already incorporated; preservation/install evidence in `docs/t01-engineering-preservation.md` |
| #14 | `758b570` | Already incorporated |
| #15 | `6d9c4cf` | Already incorporated |
| #16 | `6addd62` | Already incorporated |
| #17 | `a3fdc8f` | Already incorporated |
| #18 | `db34238` | Integrated with additional review fixes |
| #19 | `2e61c3a` | Already incorporated |
| #20 | `beece69` | Already incorporated |
| #21 | `f89c140` | Already incorporated |
| #22 | `5b6e7dd` | Integrated with additional review and cross-domain backup fixes |
| #23 | `9059780` | Already incorporated |
| #24 | `49237e761a47b6d7baa38e610351118d3bfaac6a` | Reviewed implementation integrated |
| #25 | `39c040f5af50cf3156365c597d694e5eeb60f3d1` | Code integrated; ticket remains open for its explicit real Obsidian acceptance |
| #27 | `89e357990921eab9194793957e3a24edb537d9a8` | Reviewed implementation integrated; Review backup connected |
| #28 | `02a47afa7aad109b5d541e4f80c388bff4fdad46` | Reviewed implementation integrated; Practice backup connected |
| #31 | `945ca9d` | Already incorporated; independent fake-service protocol evidence |
| #32 | `33c7e34` | Already incorporated; paid/English production branch remains pending |
| #33 | `11aa344` | Already incorporated; real upload remains pending authorization and acceptance |

## Acceptance boundaries

- #25 explicitly requires actual Obsidian wide/narrow layout, stable-section
  jumps, screenshots, generation interruption behavior, and plugin lifecycle
  evidence. Browser or CLI tests cannot complete that criterion. Keep #25 open.
- #38 (T26) owns real teaching quality, user-written practice, next-day learning
  and real daily-review experience. Those outcomes are not inferred from tests.
- #39 (T27) owns final A01–A18 evidence and all-domain real recovery, including
  later cards and delivery receipts. Current backup tests cover implemented domains.
- #32/#33 production integrations remain pending as allowed by their tickets;
  no payment, account access, upload, or reminder enablement is authorized here.
- No real learning library, Obsidian Vault, global installation, or private user
  data is modified by this closeout. Tests use isolated generated fixtures.

## Next independent tasks

After the implementation tickets are closed, query native GitHub dependencies
again before claiming work. Expected unblocked implementation tickets are #29
(cards and scheduling, depends on #27) and #36 (migration tooling, depends on
#18/#20/#22). Real migration in #36 still requires separate authorization.

#26 depends on both #24 and #25 and therefore remains blocked by #25 acceptance.
Do not start #30/#34/#35/#37/#38/#39 before their native blockers are satisfied.
Start each new ticket from the published integration branch, in its own worktree
and `codex/` branch; preserve other sessions' work. Use specification, ticket,
ADR, commit and this evidence record as the handoff, not conversation memory.

## Closeout fixes and independent review

- #18: `2773ca7`, `10f9e01`, and `fe99fd7` bind visual approval to publication
  and history, normalize cue locators without modifying transcripts, persist
  individual frame progress, isolate adapter caches, omit adapter secrets,
  protect preparation paths, and preserve approved images after restore.
  Formal Markdown links resolve to portable immutable image objects; mere
  filename mentions, comments and code blocks do not count as adoption.
  Independent final review at `fe99fd7`: Standards 0, Spec 0; 45 focused tests.
- #22: `db86aca` permits genuinely unused authorities while rejecting corrupt
  published stores and revalidates the copied restore stage against the pinned
  backup manifest. `75506b4` connects Review and Practice to actual backup and
  restore, deduplicating shared Learn objects and accepting earlier v1 backups.
  `15daeff` rejects malformed receipt roots. `f66c69f` captures stable saved
  Practice edits before backup and rechecks them under the domain locks.
  Independent final review of those fixes: Standards 0, Spec 0.
- Previously supplied #24/#25/#27/#28 branch reviews remain part of the
  evidence. Integration preserves their CLI commands, capabilities, schemas,
  plugin and installation entries; 25 Learn/view/Review combination tests and
  63 Practice/Review/capability/install tests passed during serial integration.

Shared contracts are additive: notes-snapshot-v1 gains optional visual approval
facts, and backup-manifest-v1 gains optional Review/Practice authorities and
explicit entry paths. Capability IDs and public versions remain unchanged.
Old valid backup manifests remain readable. Backup now saves stable live
Practice edits through its checkpoint contract before pinning the backup.

## Verification

All tests use generated temporary workspaces and synthetic sources, including
timed transcripts, PNG evidence, Markdown explanations, Recall events and
saved Python practice text. Public CLI operations verify expected statuses,
unchanged Learn facts, durable links, missing/corrupt inputs, interrupted
publication, result-only restore and restored histories/content.

- Python compileall: passed.
- JSON Schema meta-validation: all 21 schemas passed.
- Obsidian plugin JavaScript syntax: passed.
- `git diff --check e76327b...HEAD`: passed.
- Wheel build: passed; 98 archive entries, all 21 schemas and the new
  implementation modules/plugin verified present.
- Full pytest at code commit `fe99fd7f6101b8b3509094c0662deb5db91b4496`:
  **387 passed, 1 skipped, 11 subtests passed**, 81.56 seconds.
  Command: `PYTHONPATH=. python -m pytest -q`, using the existing project venv.

## Copyable next-ticket instruction

Replace `<ticket>` with an unblocked ticket number (initially 29 or 36):

```text
$implement 实现 GitHub 200166shang/learn-anything-workflow 的 #<ticket>。
先读取票、#12、CONTEXT.md、相关 ADR 和
docs/acceptance/completed-ticket-handoff.md，并核实 GitHub 原生阻塞关系。
从最新 origin/codex/issue-12-learning-system 创建独立 worktree 和 codex/ 分支。
复用已整合成果，不重新实现前置票；不操作其他会话的 worktree。
按票规定的公开接口执行适用 TDD，完成 Standards/Spec 独立 review、
全量测试、Schema、打包与差异检查，修复所有 findings。
提交推送，创建目标为 codex/issue-12-learning-system 的票级 PR，
报告测试、共享契约和待验收项；合并由单一整合会话串行处理。
不关闭 #12，不将总 PR #40 标记 ready，不自动关闭未验收的票。
不操作真实学习库或全局安装；真实迁移、付费、上传、提醒启用另行授权。
```
