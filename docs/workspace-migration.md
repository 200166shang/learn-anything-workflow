# Workspace migration

The workspace contains three sibling boundaries: `video-extract-core/`, `media/`, and the independent `学习系统/` Obsidian Vault. `workspace.toml` is their portable root contract. Only the locator at `~/.config/video-extract/config.toml` contains a machine-specific absolute path.

For a new computer:

1. Copy `media/items`, `media/collections`, and the complete Vault. Clone the code project.
2. Copy `workspace.example.toml` to the common parent as `workspace.toml` and verify its relative paths.
3. Write `workspace = "/absolute/path/to/workspace.toml"` in `~/.config/video-extract/config.toml`.
4. Run `uv sync`, then `uv tool install --force --editable /path/to/video-extract-core`.
5. Run `video-extract workspace doctor --json` and `video-extract workspace rebuild --dry-run --json`.
6. Only after the dry-run passes, run `video-extract workspace rebuild --apply --json` and repeat doctor.

Do not copy SQLite, playback, or `资料库/网站视频转录`; rebuild them. Never overwrite `threads/`, `concepts/`, `REVIEW.md`, or `收件箱/`. Retain migration quarantine until a separately approved cleanup.
