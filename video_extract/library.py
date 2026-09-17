"""Rebuildable SQLite FTS5 index for Media learning packages."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import os
import tempfile
from pathlib import Path
from typing import Any

from .manifest import read_json
from .validate import validate, validate_goals, validate_media_package_state


SCHEMA = """
CREATE TABLE IF NOT EXISTS items(item_id TEXT PRIMARY KEY, platform TEXT, collection_id TEXT, collection_title TEXT, section TEXT, ordinal INTEGER, title TEXT, topics_json TEXT, status TEXT, duration_seconds REAL, package_path TEXT, note_path TEXT, transcript_path TEXT, obsidian_path TEXT, updated_ns INTEGER);
CREATE TABLE IF NOT EXISTS documents(document_id TEXT PRIMARY KEY, item_id TEXT, kind TEXT, title TEXT, heading TEXT, start_seconds REAL, end_seconds REAL, body TEXT, source_path TEXT, source_hash TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(title, heading, body, content='documents', content_rowid='rowid', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN INSERT INTO documents_fts(rowid,title,heading,body) VALUES(new.rowid,new.title,new.heading,new.body); END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN INSERT INTO documents_fts(documents_fts,rowid,title,heading,body) VALUES('delete',old.rowid,old.title,old.heading,old.body); END;
"""


def _db(root: Path) -> Path:
    return root.expanduser().resolve() / "catalog/library.sqlite"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _time(value: str) -> float:
    hours, minutes, rest = value.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


def _note_chunks(text: str) -> list[tuple[str, str]]:
    chunks, heading, body = [], "", []
    for line in text.splitlines():
        if line.startswith("#"):
            if body: chunks.append((heading, "\n".join(body).strip()))
            heading, body = line.lstrip("# "), []
        else: body.append(line)
    if body or heading: chunks.append((heading, "\n".join(body).strip()))
    return [(h, b) for h, b in chunks if h or b]


def _srt_chunks(text: str) -> list[tuple[float, float, str]]:
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        match = re.search(r"(\d\d:\d\d:\d\d[,.]\d+)\s+-->\s+(\d\d:\d\d:\d\d[,.]\d+)", block)
        if match:
            cues.append((_time(match.group(1)), _time(match.group(2)), " ".join(block[match.end():].strip().splitlines())))
    chunks, current = [], []
    for cue in cues:
        current.append(cue)
        if cue[1] - current[0][0] >= 30:
            chunks.append((current[0][0], cue[1], " ".join(x[2] for x in current))); current = []
    if current: chunks.append((current[0][0], current[-1][1], " ".join(x[2] for x in current)))
    return chunks


def _sources(package: Path, manifest: dict[str, Any]) -> list[Path]:
    artifacts = manifest.get("artifacts", {})
    paths = [path for key in ("notes", "transcript_srt", "source_subtitle", "source_document") if (path := package / str(artifacts.get(key, ""))).is_file()]
    paths.extend(path for path in package.glob("pyvideotrans-full/sts/*zh-cn.complete.srt") if path.is_file())
    return list(dict.fromkeys(paths))


def _combined_hash(package: Path, manifest: dict[str, Any]) -> str:
    digest = hashlib.sha256((package / "manifest.json").read_bytes())
    for path in _sources(package, manifest): digest.update(path.read_bytes())
    return digest.hexdigest()


def _index(connection: sqlite3.Connection, package: Path) -> None:
    manifest = read_json(package / "manifest.json"); item_id = str(manifest.get("identity") or package.name)
    artifacts = manifest.get("artifacts", {}); note = package / str(artifacts.get("notes", "")); transcript = package / str(artifacts.get("transcript_srt") or artifacts.get("source_subtitle") or "")
    connection.execute("DELETE FROM documents WHERE item_id=?", (item_id,)); connection.execute("DELETE FROM items WHERE item_id=?", (item_id,))
    if manifest.get("schema_version") == 5 and manifest.get("request", {}).get("type") == "media":
        checked = validate_media_package_state(package); validation_status = "valid_complete" if checked.get("ok") and checked.get("complete") else "valid_partial" if checked.get("ok") else "preexisting_invalid"
    elif manifest.get("schema_version") == 5 and manifest.get("request", {}).get("type") == "source":
        checked = validate_goals(package, ["notes_zh"]); validation_status = "valid_complete" if checked.get("ok") else "valid_partial"
    else:
        checked = validate(package).to_dict(); validation_status = "legacy_preserved_invalid" if checked.get("invalid") else "valid_complete" if checked.get("observed_gate") in {"notes_complete", "summary_complete"} else "valid_partial"
    export_manifest = package / "export-manifest.json"; exported = read_json(export_manifest).get("note") if export_manifest.is_file() else manifest.get("obsidian_path")
    connection.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (item_id, manifest.get("platform"), manifest.get("collection_id"), manifest.get("collection") or manifest.get("collection_title"), manifest.get("section"), manifest.get("ordinal"), manifest.get("title"), json.dumps(manifest.get("topics") or [], ensure_ascii=False), validation_status, manifest.get("duration_seconds"), str(package), str(note) if note.is_file() else None, str(transcript) if transcript.is_file() else None, exported, max((p.stat().st_mtime_ns for p in _sources(package, manifest)), default=(package / "manifest.json").stat().st_mtime_ns)))
    if note.is_file():
        source_hash = _hash(note)
        for index, (heading, body) in enumerate(_note_chunks(note.read_text(encoding="utf-8"))):
            connection.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)", (f"{item_id}:note:{index}", item_id, "note", manifest.get("title"), heading, None, None, body, str(note), source_hash))
    if transcript.is_file():
        source_hash = _hash(transcript)
        for index, (start, end, body) in enumerate(_srt_chunks(transcript.read_text(encoding="utf-8-sig"))):
            connection.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)", (f"{item_id}:transcript:{index}", item_id, "transcript", manifest.get("title"), "", start, end, body, str(transcript), source_hash))
    document = package / str(artifacts.get("source_document", ""))
    if document.is_file():
        source_hash = _hash(document)
        for index, (heading, body) in enumerate(_note_chunks(document.read_text(encoding="utf-8-sig"))):
            connection.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)", (f"{item_id}:source:{index}", item_id, "source", manifest.get("title"), heading, None, None, body, str(document), source_hash))
    for extra in (path for path in _sources(package, manifest) if path.suffix.lower() == ".srt" and path != transcript):
        source_hash = _hash(extra)
        for index, (start, end, body) in enumerate(_srt_chunks(extra.read_text(encoding="utf-8-sig"))):
            connection.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)", (f"{item_id}:transcript-extra:{_hash(extra)[:8]}:{index}", item_id, "transcript", manifest.get("title"), "", start, end, body, str(extra), source_hash))
    connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", (f"hash:{item_id}", _combined_hash(package, manifest)))


def rebuild_library(library_root: Path) -> dict[str, Any]:
    library_root = library_root.expanduser().resolve(); database = _db(library_root); database.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".library-", suffix=".sqlite", dir=database.parent)
    os.close(descriptor); temporary = Path(temporary_name)
    try:
        with sqlite3.connect(temporary) as connection:
            connection.executescript(SCHEMA + "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);")
            packages = sorted(path.parent for path in (library_root / "items").glob("*/*/manifest.json"))
            for package in packages: _index(connection, package)
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok": raise RuntimeError("SQLite integrity check failed")
        os.replace(temporary, database)
    finally:
        temporary.unlink(missing_ok=True)
    return {"ok": True, "indexed_items": len(packages), "database": str(database)}


def update_package(package: Path, library_root: Path) -> dict[str, Any]:
    package = package.expanduser().resolve(); database = _db(library_root)
    if not database.exists(): rebuild_library(library_root)
    manifest = read_json(package / "manifest.json"); item_id = str(manifest.get("identity") or package.name); current = _combined_hash(package, manifest)
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT value FROM metadata WHERE key=?", (f"hash:{item_id}",)).fetchone()
        if row and row[0] == current: return {"ok": True, "updated": False, "item_id": item_id}
        _index(connection, package)
    return {"ok": True, "updated": True, "item_id": item_id}


def search_library(query: str, library_root: Path, limit: int = 10, platform: str | None = None, collection: str | None = None, section: str | None = None, status: str | None = None) -> dict[str, Any]:
    database = _db(library_root)
    if not database.exists(): return {"ok": True, "query": query, "results": []}
    filters, params = ["documents_fts MATCH ?"], [query]
    for column, value in (("i.platform", platform), ("i.collection_title", collection), ("i.section", section), ("i.status", status)):
        if value is not None: filters.append(column + "=?"); params.append(value)
    params.append(limit)
    sql = f"SELECT d.item_id,d.kind,d.title,d.heading,d.start_seconds,d.end_seconds,snippet(documents_fts,2,'[',']','…',20),i.package_path,i.obsidian_path FROM documents_fts JOIN documents d ON d.rowid=documents_fts.rowid JOIN items i ON i.item_id=d.item_id WHERE {' AND '.join(filters)} ORDER BY bm25(documents_fts) LIMIT ?"
    with sqlite3.connect(database) as connection:
        rows = connection.execute(sql, params).fetchall()
        if not rows:
            compact = re.sub(r"\s+", "", query)
            windows = list(dict.fromkeys(compact[index:index + 3] for index in range(max(0, len(compact) - 2))))
            if windows:
                fallback = " OR ".join('"' + value.replace('"', '""') + '"' for value in windows)
                fallback_params = [fallback, *params[1:]]
                rows = connection.execute(sql, fallback_params).fetchall()
        if not rows:
            compact = re.sub(r"\s+", "", query); terms = list(dict.fromkeys(compact[index:index + 2] for index in range(max(0, len(compact) - 1))))
            if terms:
                where = ["(" + " OR ".join("d.body LIKE ?" for _ in terms) + ")"]; like_params: list[Any] = [f"%{term}%" for term in terms]
                for column, value in (("i.platform", platform), ("i.collection_title", collection), ("i.section", section), ("i.status", status)):
                    if value is not None: where.append(column + "=?"); like_params.append(value)
                score = " + ".join("CASE WHEN d.body LIKE ? THEN 1 ELSE 0 END" for _ in terms)
                like_params = like_params + [f"%{term}%" for term in terms] + [limit]
                fallback_sql = f"SELECT d.item_id,d.kind,d.title,d.heading,d.start_seconds,d.end_seconds,substr(d.body,1,240),i.package_path,i.obsidian_path FROM documents d JOIN items i ON i.item_id=d.item_id WHERE {' AND '.join(where)} ORDER BY ({score}) DESC LIMIT ?"
                rows = connection.execute(fallback_sql, like_params).fetchall()
    keys = ("item_id", "kind", "title", "heading", "start_seconds", "end_seconds", "snippet", "package_path", "obsidian_path")
    return {"ok": True, "query": query, "results": [dict(zip(keys, row)) for row in rows]}


def library_status(library_root: Path) -> dict[str, Any]:
    database = _db(library_root)
    if not database.exists(): return {"ok": False, "indexed_items": 0, "stale_documents": 0, "missing_paths": 0}
    stale = missing = 0
    with sqlite3.connect(database) as connection:
        items = connection.execute("SELECT item_id,package_path FROM items").fetchall()
        for item_id, package_text in items:
            package = Path(package_text)
            if not (package / "manifest.json").is_file(): missing += 1; continue
            manifest = read_json(package / "manifest.json")
            stored = connection.execute("SELECT value FROM metadata WHERE key=?", (f"hash:{item_id}",)).fetchone()
            if not stored or stored[0] != _combined_hash(package, manifest): stale += 1
        for (source,) in connection.execute("SELECT DISTINCT source_path FROM documents"):
            if not Path(source).is_file(): missing += 1
    return {"ok": stale == 0 and missing == 0, "indexed_items": len(items), "stale_documents": stale, "missing_paths": missing, "database": str(database)}


def audit_library_packages(library_root: Path) -> dict[str, Any]:
    categories = {name: [] for name in ("valid_complete", "valid_partial", "legacy_preserved_invalid", "preexisting_invalid", "migration_regression")}
    for manifest_path in sorted((library_root.expanduser().resolve() / "items").glob("*/*/manifest.json")):
        package = manifest_path.parent
        try:
            manifest = read_json(manifest_path)
            if manifest.get("schema_version") == 5 and manifest.get("request", {}).get("type") == "media":
                result = validate_media_package_state(package)
                category = "valid_complete" if result.get("ok") and result.get("complete") else "valid_partial" if result.get("ok") else "preexisting_invalid"
            else:
                result = validate(package).to_dict()
                category = "legacy_preserved_invalid" if result.get("invalid") else "valid_complete" if result.get("observed_gate") in {"notes_complete", "summary_complete"} else "valid_partial"
        except Exception as exc:
            result = {"error": str(exc)}; category = "preexisting_invalid"
        categories[category].append({"package": str(package), "validation": result})
    return {"ok": not categories["migration_regression"], "counts": {key: len(value) for key, value in categories.items()}, "categories": categories}
