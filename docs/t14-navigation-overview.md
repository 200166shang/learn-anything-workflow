# T14 Module overview and maintained navigation

Issue: `#26`. Specification: `#12` (US17, US21–US23; I3–I6; A05, A06, A16, A17).

## Public contract

- `video-extract view build --module-id ...` publishes one immutable generation containing a local question graph, root-grouped module overview, searchable question catalog, and every reachable complete explanation projection.
- The local graph contains only the active thread plus its cross-root reference targets. The overview and catalog contain every root and question owned by the module. Cross-root reuse is rendered as a dashed boundary from the local question to the referenced source question; identities are linked rather than copied or recursively expanded.
- The three HTML views, documents, locators, generation ID, and learning commit are hashed and published together. Any failed build leaves the previous pointer readable and records `unsynced`; modified immutable files are preserved for inspection rather than overwritten.
- `view status` validates every view and projected document. `view locate` opens the complete generation-pinned document at its stable section. `workspace rebuild` discovers every module from learning-record-v2 and rebuilds these derived views without changing Learn facts.
- The project-owned Obsidian plugin switches among all three read-only views. Catalog filtering runs locally; navigation delegates only complete explanation links back to Obsidian.

## Acceptance boundary

Automated fixtures cover two roots, active-thread locality, full-generation hashes, semantic search rows, feedback-driven regeneration, manual-edit preservation, cross-root dashed edges, public rebuild planning, and plugin lifecycle source checks. They do not replace the real Obsidian evidence below. Final end-to-end acceptance remains tracked by `#39`; `#41` may add non-blocking visual polish without reopening this carrier contract.

`tools/prepare_issue26_vault.py TARGET` reproducibly creates an isolated two-root, two-generation Vault containing all three views for real Obsidian switching, search, navigation, and version acceptance. It never touches the user's production learning library.

## Real Obsidian evidence (2026-09-17)

- Engine: Obsidian 1.13.7; plugin 0.2.0; generation Schema v2; source revision `9528e7a` plus this ticket's worktree.
- Input: isolated module `module-8f837632-a018-4d67-adca-214d3411f400`, two roots, five module-owned questions, one complete explanation, pending/confused/parked states, and three immutable generations. No production learning data was used.
- Public operations: generated the Vault with `tools/prepare_issue26_vault.py`; published a feedback change with `video-extract learning feedback`; rebuilt with `video-extract view build`; opened the project command “打开只读学习导航”.
- Expected/actual: local graph, root-grouped relationship overview, and catalog switched inside one open Obsidian view; catalog search `机器人` left exactly the two matching rows; the available question opened the full generation-pinned explanation at `section-4d971453-84fa-4062-8cee-6ad76f21f124`.
- Concurrent publish: while `view-generation-e4611018b84239c3a89ab575dad7a9d4546d07fc4b9de5fd489ddabe9dc84174` remained open, `view-generation-f93b7e6f02466044cd53a676a993039c28568d5f0d13edb221532085b69473ca` was published. The old content remained readable and changed its banner to “正在查看旧版本”; closing and reopening loaded the new generation with `learning-commit-01d9ce5fdfb5db7fc73c9b7c87768b48da40948cc365433b09eced70b296cd72` atomically.
- Disposition: passed for issue #26's Obsidian carrier. Broader final end-to-end acceptance remains owned by #39; no simulated check is presented as that final acceptance.

The live acceptance screenshot was captured in the implementation task while the synthetic Vault was open; its content-addressed evidence record is [`docs/evidence/t14-obsidian-ui.json`](evidence/t14-obsidian-ui.json). The accessibility snapshot records all three view buttons, both root groups, the relationship SVG, generation and learning commit IDs. The screenshot contains no production data. Against fixed prototype commit `44a5fb1`, the accepted carrier preserves the decided invariants: resume position and return route, independent feedback/content state, current-node emphasis, solid pursuit and dashed reference semantics, local focus, root-grouped overview, read-only click behavior, and narrow-width overflow. Exact pixel coordinates were intentionally not treated as a contract, matching the prototype README.
