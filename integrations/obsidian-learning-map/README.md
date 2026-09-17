# Video Extract Learning Map plugin

Publish this project-owned directory with
`video-extract install apply --obsidian-plugins-root VAULT/.obsidian/plugins`,
enable the plugin, and run
**打开只读学习导航**. The Vault must be the workspace-v2 `derived` role and
must already contain a generation published by `video-extract view build`.

The plugin asks for a module when more than one published pointer exists, validates the
generation manifest and each rendered view hash, switches among the generation-pinned
local graph, root-grouped module overview, and searchable question catalog, then delegates
explanation links to Obsidian. It does not write learning feedback, position, explanations,
or generated files. An open old generation remains readable and reports newer or failed
publication state until the navigation view is reopened.
