# Issue 25 real Obsidian acceptance runbook

This is the manual acceptance entry point for `learn-anything-workflow#25`.
Automated tests prove the projection contract, but do not satisfy the ticket's
requirement to exercise the project-owned plugin in real Obsidian.

## Do not use the current production Vault

`view build` requires workspace schema v2 and publishes below its `derived`
role. The current `/Users/syz/VideoLearning/workspace.toml` is schema v1 and
points at the existing `学习系统` Vault. Do not install the acceptance plugin
there or change that file merely to run this check.

Use a separate schema-v2 workspace whose `derived` directory is an isolated
Obsidian Vault. It must contain a persisted Learn snapshot, not hand-written
HTML. The fixed F-navigation example needs an actual return route, a current
question with `parked` feedback, one complete explanation with a stable section,
one pending explanation, and one broken locator.

If no such workspace exists, acceptance is **blocked on fixture preparation**.
The repository includes a one-step visual-review fixture builder:

```bash
PYTHONPATH=. ./.venv/bin/python tools/prepare_issue25_vault.py \
  /absolute/path/to/new-empty-acceptance-directory
```

It creates the schema-v2 workspace, generated view, plugin installation, and an
`acceptance.json` containing the IDs. Open its `obsidian-vault` directory in
Obsidian. This starter covers the real plugin, available/pending nodes, route,
position, parked feedback, and exact-section navigation. A reproducible broken
locator plus cross-root fixture is still required for the complete ticket; do
not turn this partial visual review into a full pass.

## 1. Preflight and build

Use explicit paths so commands cannot discover the schema-v1 workspace:

```bash
cd /Users/syz/VideoLearning/video-extract-core
ACCEPTANCE_WORKSPACE=/absolute/path/to/issue-25-workspace/workspace.toml
ACCEPTANCE_VAULT=/absolute/path/to/issue-25-workspace/obsidian-vault
MODULE_ID=module-...
AVAILABLE_QUESTION_ID=question-...
PENDING_QUESTION_ID=question-...
BROKEN_QUESTION_ID=question-...

./.venv/bin/video-extract workspace show \
  --workspace "$ACCEPTANCE_WORKSPACE" --json
./.venv/bin/video-extract view build --module-id "$MODULE_ID" \
  --workspace "$ACCEPTANCE_WORKSPACE" --json
./.venv/bin/video-extract view status --module-id "$MODULE_ID" \
  --workspace "$ACCEPTANCE_WORKSPACE" --json
./.venv/bin/video-extract view locate "$AVAILABLE_QUESTION_ID" \
  --workspace "$ACCEPTANCE_WORKSPACE" --json
./.venv/bin/video-extract view locate "$PENDING_QUESTION_ID" \
  --workspace "$ACCEPTANCE_WORKSPACE" --json
./.venv/bin/video-extract view locate "$BROKEN_QUESTION_ID" \
  --workspace "$ACCEPTANCE_WORKSPACE" --json
```

Stop unless `workspace show` reports schema 2 and its `derived` path is exactly
the isolated Vault. Expected results are: build `completed`; status `current`;
the available question returns a complete Markdown path and an
`obsidian_href` ending in `#^section-...`; the others return distinct `pending`
and `broken` states. A broken locator must never fall back to the document start.

Record `git rev-parse HEAD`, the JSON output, the Obsidian version, and plugin
version. The JSON supplies generation ID, Learn commit, and exact target.

## 2. Install only in the isolated Vault

```bash
PLUGIN_DIR="$ACCEPTANCE_VAULT/.obsidian/plugins/video-extract-learning-map"
mkdir -p "$PLUGIN_DIR"
cp integrations/obsidian-learning-map/manifest.json "$PLUGIN_DIR/"
cp integrations/obsidian-learning-map/main.js "$PLUGIN_DIR/"
cp integrations/obsidian-learning-map/styles.css "$PLUGIN_DIR/"
node --check "$PLUGIN_DIR/main.js"
```

In Obsidian choose **Manage vaults… → Open folder as vault** and select
`$ACCEPTANCE_VAULT`. In **Settings → Community plugins**, enable
**Video Extract Learning Map**. Reload Obsidian if it was open while copying.
Open the command palette and run
**Video Extract Learning Map: 打开只读局部问题图**. This command, rather than a
file in the navigator, is the supported UI entry point.

## 3. Real UI checks

1. At a wide size, capture the header and graph. Confirm **续学位置**,
   **本次返回路线**, and **暂放入口**; current position is separate from feedback;
   actual-question edges are solid and cross-root references are dashed.
2. Narrow the pane to about 500 px. The header stays readable and the graph
   scrolls horizontally rather than clipping or overlapping.
3. Click the available node. Obsidian opens the complete explanation at the
   exact `^section-...` returned by `view locate`, not a preview or file start.
4. Pending displays **待讲解**; bad locator displays **定位待修复**. Neither
   masquerades as a working link.
5. Compare the Learn snapshot before and after clicking. Its commit is unchanged.
6. Disable the plugin: its custom view closes. Re-enable it and reopen the view.
7. Read the projected Markdown without the graph and confirm its title, context,
   body, and source information are understandable as a standalone explanation.

## 4. Generation isolation

Advance one fixture Learn fact, then verify that the view is stale:

```bash
./.venv/bin/video-extract learning feedback \
  --question-id "$BROKEN_QUESTION_ID" --state confused \
  --text "验收：定位仍需修复" --confusion "稳定 section 不可解析" \
  --workspace "$ACCEPTANCE_WORKSPACE" --json
./.venv/bin/video-extract view status --module-id "$MODULE_ID" \
  --workspace "$ACCEPTANCE_WORKSPACE" --json
```

The feedback response must contain a new Learn commit, and status must be
`unsynced` while Obsidian still shows the old, internally consistent generation.
During an interrupted fixture build, the old pointer must remain selected; a new
graph must never mix with old Markdown. After a successful build, graph,
documents, manifest, generation ID, and Learn commit advance together.

For the controlled interruption check in this disposable fixture, run:

```bash
VIDEO_EXTRACT_VIEW_TEST_FAULT=before_publish \
  ./.venv/bin/video-extract view build --module-id "$MODULE_ID" \
  --workspace "$ACCEPTANCE_WORKSPACE" --json
./.venv/bin/video-extract view status --module-id "$MODULE_ID" \
  --workspace "$ACCEPTANCE_WORKSPACE" --json
```

This fault-injection environment variable is not a public operation. Label its
result controlled engineering evidence, not user workflow.

To check that manual changes are not overwritten, make a backup of the generated
graph, append a visible marker to the served graph, and rerun `view build`. The
build must fail recoverably, preserve the marker for inspection, and report
`unsynced`. Restore the backup, then build successfully before continuing. Do
this only in the disposable Vault; record that the projected Markdown was also
readable independently.

## 5. Evidence and verdict

Attach or link these non-sensitive facts in issue #25:

| Evidence | Required contents |
| --- | --- |
| Versions | engineering commit, workspace/manifest schemas, Obsidian and plugin versions |
| Input | fixture module ID and Learn/source version; no private source text |
| Operations | exact `view build/status/locate` and plugin command |
| UI | wide/narrow screenshots, relation styles, responsive scrolling |
| Navigation | source node, complete explanation, exact stable section |
| States | separate pending and broken-locator displays |
| Safety | unchanged Learn commit, unload/reload, generation isolation, preserved manual edit |
| Document | complete projected Markdown is independently readable |
| Verdict | pass/fail/pending for every row |

Only pass the real-Obsidian criterion when every UI row has desktop evidence.
For a failure, record Obsidian version, generation ID, expected and actual
behavior, and one reproduction step. Never turn pending real checks into an
automated pass.
