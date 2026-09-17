# T13: Obsidian local question map and exact explanation navigation

Issue: `learn-anything-workflow#25`.

The public seams are `view build --module-id`, `view status --module-id`, and
`view locate QUESTION_ID`. The projection reads one immutable `learning-snapshot-v2`
commit and publishes one immutable view generation below the workspace-v2 `derived`
role. Its manifest pins the learning commit, a digest of the selected module roots,
each explanation object digest, and each stable section locator. The current pointer
changes only after the graph, complete Markdown documents, and manifest are ready.

The local graph shows the current position separately from explicit feedback, the
actual return route, parked entry, actual-question edges, and dashed cross-root
references. Pending explanations and broken locators are different content states.
`view locate` refuses to fall back to the start of a document: it returns either the
complete projected document plus its exact Obsidian block section, `pending`, or
`broken`.

The projection never writes `learning-record-v2`. A deleted generation can be rebuilt
from the pinned authoritative record. Interrupted builds preserve the previously
served generation, while `view status` reports `unsynced`; intact immutable output is
reused rather than overwritten, preserving unexpected manual changes for inspection.

`integrations/obsidian-learning-map` is the project-owned, read-only desktop plugin.
It discovers the published module pointer, opens the local graph, delegates node links
to Obsidian, and unregisters its view/resources on unload. It must be installed into an
isolated fixture Vault for real UI acceptance. Automated tests use only temporary
workspace fixtures, so responsive rendering, plugin install/uninstall, and click
behavior in real Obsidian remain pending acceptance under A06/A16.

The isolated-Vault operator checklist and evidence template are in
[`docs/acceptance/issue-25.md`](acceptance/issue-25.md). The current schema-v1
production Vault is not a valid acceptance target and must not be modified to
manufacture this evidence. A reproducible public fixture-preparation entry point
is still required before a clean checkout can run the complete checklist.
