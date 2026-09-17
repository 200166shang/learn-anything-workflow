from video_extract.cutover import classify_processes, parse_lsof


def test_read_only_handle_is_warning_not_writer() -> None:
    result = parse_lsof(["node 30094 syz 16r REG 1,14 1877 11814083 /vault/note.md"])
    assert result["active_writers"] == []
    assert result["read_only_handles"][0]["pid"] == 30094


def test_write_and_update_handles_block() -> None:
    result = parse_lsof(["python 42 syz 4w REG 1,14 1 2 /vault/a", "node 43 syz 7u REG 1,14 1 3 /vault/b"])
    assert [entry["pid"] for entry in result["active_writers"]] == [42, 43]


def test_obsidian_and_pipeline_processes_block_but_codex_web_server_does_not() -> None:
    lines = [
        "10 1 /Applications/Obsidian.app/Contents/MacOS/Obsidian",
        "11 1 python -m video-extract workspace rebuild --apply",
        "12 1 node /Users/syz/.codex/skills/web/server/index.mjs /vault/example",
    ]
    blockers = classify_processes(lines)
    assert [item["pid"] for item in blockers] == [10, 11]
