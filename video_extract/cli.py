"""Stable command-line interface for authorized video-learning work."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .contracts import SCHEMA_VERSION
from .manifest import atomic_write_json, fingerprint, read_json, relative_path, sanitize
from .media_request import normalize_media_request
from . import media_workflow
from .orchestrator import existing_capabilities
from .playlists import build_playback_views, build_xiaoe_playback_views, verify_playback
from .validate import validate, validate_goals, validate_media_request
from .workspace import discover_workspace, doctor as workspace_doctor, prepare_migration, rebuild as workspace_rebuild

PROJECT = Path(__file__).resolve().parent.parent


def emit(value: Any, as_json: bool) -> None:
    if as_json: print(json.dumps(sanitize(value), ensure_ascii=False, indent=2))
    elif isinstance(value, dict):
        for key, item in value.items(): print(f"{key}: {item}")
    else: print(value)


def run_legacy(script: str, arguments: list[str], as_json: bool = False) -> int:
    script_path = PROJECT / script
    if script_path.exists():
        command = [sys.executable, str(script_path), *arguments]
    else:
        command = [sys.executable, "-m", script.removesuffix(".py"), *arguments]
    completed = subprocess.run(command, capture_output=as_json, text=as_json)
    if as_json:
        emit({"ok": completed.returncode == 0, "adapter": script.removesuffix(".py"), "result": (completed.stdout or "").strip(), "error": (completed.stderr or "").strip() or None}, True)
    return completed.returncode


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = {name: bool(shutil.which(name)) for name in ("ffmpeg", "ffprobe", "yt-dlp")}
    if sys.platform == "darwin":
        checks["say"] = Path("/usr/bin/say").is_file()
    try:
        import PIL  # noqa: F401
        checks["pillow"] = True
    except ImportError: checks["pillow"] = False
    try:
        import playwright  # noqa: F401
        checks["playwright"] = True
    except ImportError:
        checks["playwright"] = False
    result = {"ok": all(checks.values()), "schema_version": SCHEMA_VERSION, "checks": checks, "project": str(PROJECT)}
    emit(result, args.json); return 0 if result["ok"] else 1


def cmd_verify(args: argparse.Namespace) -> int:
    manifest = read_json(args.path / "manifest.json") if (args.path / "manifest.json").exists() else {}
    if manifest.get("schema_version") == 5 and manifest.get("request", {}).get("type") == "media":
        result = validate_media_request(args.path); emit(result, args.json); return 0 if result["ok"] else 1
    if getattr(args, "goal", None):
        result = validate_goals(args.path, args.goal); emit(result, args.json); return 0 if result["ok"] else 1
    result = validate(args.path, args.catalog).to_dict(); emit(result, args.json); return 0 if not result["missing"] and not result["invalid"] else 1


def _discover(root: Path) -> list[dict[str, Any]]:
    found = []
    for path in root.expanduser().resolve().rglob("manifest.json"):
        found.append(validate(path.parent).to_dict())
    return found


def cmd_status(args: argparse.Namespace) -> int:
    path = args.path.expanduser().resolve()
    if (path / "manifest.json").exists() or any((path / name).exists() for name in ("collection.json", "course_catalog.json", "catalog.json")):
        manifest = read_json(path / "manifest.json") if (path / "manifest.json").exists() else {}
        goals = manifest.get("request", {}).get("goals")
        result: Any = validate_media_request(path) if manifest.get("schema_version") == 5 and manifest.get("request", {}).get("type") == "media" else {
            "package": str(path), "requested_goals": goals,
            "goal_status": validate_goals(path, goals),
            "reusable_capabilities": sorted(existing_capabilities(path)),
        } if goals else validate(path, args.catalog).to_dict()
    else:
        packages = _discover(path)
        result = {"root": str(path), "catalog_known": False, "discovered_packages": len(packages), "packages": packages,
                  "note": "No catalog was found; not-started count is unknown."}
    emit(result, args.json); return 0


def cmd_plan(args: argparse.Namespace) -> int:
    if args.request:
        from .command_response import exit_code
        from .media_operations import plan_request
        result = plan_request(json.loads(args.request.read_text(encoding="utf-8")))
        emit(result, args.json); return exit_code(result)
    result = media_workflow.plan(args.source, normalize_media_request(args.media, args.language, args.quality), args.output)
    emit(result, args.json); return 0 if not result.get("blockers") else 1


def cmd_ensure(args: argparse.Namespace) -> int:
    if args.request:
        from .command_response import exit_code
        from .media_operations import ensure_request
        result = ensure_request(json.loads(args.request.read_text(encoding="utf-8")))
        emit(result, args.json); return exit_code(result)
    workspace = discover_workspace(args.workspace)
    result = media_workflow.ensure(args.source, normalize_media_request(args.media, args.language, args.quality), args.output, workspace.media)
    emit(result, args.json)
    return 0 if result.get("status") == "complete" else 1


def cmd_operation(args: argparse.Namespace) -> int:
    from .command_response import exit_code
    from .media_operations import resume_operation, show_operation
    workspace = discover_workspace(args.workspace)
    result = show_operation(workspace, args.operation_id) if args.operation_action == "show" else resume_operation(workspace, args.operation_id)
    emit(result, args.json); return exit_code(result)


def cmd_source(args: argparse.Namespace) -> int:
    from .command_response import exit_code, response
    from .source_import import import_source
    from .source_registry import reconcile, register, relocate, verify
    from .workspace import PORTABLE_SCHEMA_VERSION, WorkspaceError
    workspace = discover_workspace(args.workspace)
    if args.source_action != "import" and workspace.schema_version != PORTABLE_SCHEMA_VERSION:
        result = response(status="unsupported", workspace=str(workspace.config_path),
                          validation={"workspace_schema": "failed"},
                          diagnostics=["source v6 requires workspace schema v2; old formats are not read in normal runtime"],
                          next_action={"command": "video-extract migration plan"})
        emit(result, args.json); return exit_code(result)
    try:
        if args.source_action == "register":
            result = register(workspace, args.input, args.title, args.expected_revision, args.source_id)
        elif args.source_action == "verify":
            result = verify(workspace, args.source_id, args.source_version)
        elif args.source_action == "relocate":
            result = relocate(workspace, args.source_id, args.location, args.expected_revision)
        elif args.source_action == "reconcile":
            result = reconcile(workspace, args.source_id, args.source_version)
        elif args.source_action == "import" and workspace.schema_version == PORTABLE_SCHEMA_VERSION:
            result = register(workspace, args.input, args.title, getattr(args, "expected_revision", None),
                              getattr(args, "source_id", None))
        else:
            result = import_source(args.input, workspace, args.package, args.title)
    except (OSError, ValueError, WorkspaceError) as exc:
        result = response(status="failed", workspace=str(workspace.config_path),
                          validation={"request": "failed"}, diagnostics=[str(exc)])
    emit(result, args.json)
    if args.source_action != "import" or workspace.schema_version == PORTABLE_SCHEMA_VERSION:
        return exit_code(result)
    return 0 if result.get("status") == "ready" else 1


def cmd_notes(args: argparse.Namespace) -> int:
    from .notes_workflow import finalize, prepare
    workspace = discover_workspace(args.workspace)
    result = prepare(args.package, workspace) if args.notes_action == "prepare" else finalize(args.package, workspace)
    emit(result, args.json)
    return 0 if result.get("status") in {"ready", "complete", "awaiting_ai"} else 1


def cmd_install(args: argparse.Namespace) -> int:
    from .command_response import exit_code
    from .installation import apply, inspect, plan
    agents_root = args.agents_root.expanduser().resolve()
    codex_root = args.codex_root.expanduser().resolve()
    if args.install_action == "plan":
        result = plan(agents_root, codex_root)
    elif args.install_action == "apply":
        result = apply(agents_root, codex_root, args.backup_root)
    else:
        result = inspect(agents_root, codex_root)
    emit(result, args.json)
    return exit_code(result)


def cmd_capability(args: argparse.Namespace) -> int:
    from .capabilities import check_capabilities, exit_code, list_capabilities, run_capability
    if args.capability_action == "list":
        result = list_capabilities()
    elif args.capability_action == "check":
        result = check_capabilities(args.capability_id)
    else:
        result = run_capability(args.capability_id, args.request)
    emit(result, args.json)
    return exit_code(result)


def cmd_scan(args: argparse.Namespace) -> int:
    if args.platform == "xiaoe":
        forwarded = [args.source, "--output", str(args.output)]
        if args.visible: forwarded.append("--visible")
        if args.wait_seconds is not None: forwarded += ["--wait-seconds", str(args.wait_seconds)]
        return run_legacy("scan_course.py", forwarded, args.json)
    completed = subprocess.run([sys.executable, "-m", "yt_dlp", "--flat-playlist", "--dump-single-json", args.source], capture_output=True, text=True)
    if completed.returncode:
        emit({"ok": False, "error": completed.stderr.strip()}, args.json); return completed.returncode
    raw = json.loads(completed.stdout); entries = raw.get("entries") or [raw]
    items = [{"index": i, "id": entry.get("id") or entry.get("url"), "title": entry.get("title") or "untitled", "url": entry.get("webpage_url") or entry.get("url"), "availability": entry.get("availability", "unknown")} for i, entry in enumerate(entries, 1)]
    catalog = {"schema_version": SCHEMA_VERSION, "platform": args.platform, "identity": raw.get("id") or args.source, "source_url": args.source, "title": raw.get("title"), "total": len(items), "items": items}
    atomic_write_json(args.output.expanduser().resolve(), catalog)
    checked = validate(args.output).to_dict(); ok = not checked["invalid"] and not checked["missing"]
    emit({"ok": ok, "catalog": str(args.output), "validation": checked}, args.json); return 0 if ok else 1


def cmd_acquire(args: argparse.Namespace) -> int:
    if args.platform == "youtube":
        forwarded = [args.source, "--media", args.media, "--output-dir", str(args.output)]
        if args.quality: forwarded += ["--max-height", str(args.quality)]
        return run_legacy("download_youtube.py", forwarded, args.json)
    if args.platform == "bilibili":
        if not args.urls_file:
            emit({"ok": False, "error": "Bilibili acquire requires --urls-file"}, args.json); return 2
        forwarded = [str(args.urls_file), "--output-dir", str(args.output)]
        if args.quality: forwarded += ["--quality", str(args.quality)]
        if args.workers: forwarded += ["--workers", str(args.workers)]
        if args.headless: forwarded.append("--headless")
        return run_legacy("prefetch_bilibili.py", forwarded, args.json)
    from convert_voice_to_article import capture_media, download_video
    started = time.monotonic(); output = args.output.expanduser().resolve(); source_dir = output / "source"; source_dir.mkdir(parents=True, exist_ok=True)
    captured = capture_media(args.source, args.session_dir, args.wait_seconds or 30, args.headless, getattr(args, "cdp_url", None))
    if not captured:
        emit({"ok": False, "error": "No authorized media request was captured; retry visibly after login and playback."}, args.json); return 1
    downloaded = download_video(captured[0], source_dir); canonical = source_dir / "source.mp4"
    if downloaded != canonical: downloaded.replace(canonical)
    identity = fingerprint([canonical], {"source": args.source})
    atomic_write_json(source_dir / "metadata.json", {"platform": "xiaoe", "identity": identity, "source_url": args.source, "title": captured[0].title})
    atomic_write_json(output / "manifest.json", {"schema_version": SCHEMA_VERSION, "platform": "xiaoe", "identity": identity, "title": captured[0].title, "artifacts": {"video": "source/source.mp4", "metadata": "source/metadata.json"}, "stages": {"media_ready": {"duration_ms": round((time.monotonic()-started)*1000), "input_fingerprint": identity, "parameters": {}, "cache_hit": False, "result": "passed"}}})
    checked = validate(output); ok = "media_ready" in checked.passed
    emit({"ok": ok, "package": str(output), "validation": checked.to_dict()}, args.json); return 0 if ok else 1


def cmd_xiaoe(args: argparse.Namespace) -> int:
    from .xiaoe_download import download, login, parse_chapters, status

    config = discover_workspace(args.workspace)
    if args.xiaoe_action == "login":
        result = login(config, args.course_url, args.wait_seconds)
    elif args.xiaoe_action == "download":
        result = download(config, args.course_url, parse_chapters(args.chapters), args.wait_seconds, args.visible)
    else:
        result = status(config, args.course_url)
    emit(result, args.json)
    return 0 if result.get("ok") else 1


def cmd_transcribe(args: argparse.Namespace) -> int:
    if args.catalog:
        forwarded = ["--catalog", str(args.catalog), "--output-dir", str(args.output)]
        for key in ("section", "model", "language", "task"):
            value = getattr(args, key, None)
            if value: forwarded += [f"--{key}", str(value)]
        if args.limit: forwarded += ["--limit", str(args.limit)]
        if args.workers: forwarded += ["--workers", str(args.workers)]
        if args.visible: forwarded.append("--visible")
        return run_legacy("batch_process.py", forwarded, args.json)
    forwarded = ["--video", str(args.video), "--output-dir", str(args.output), "--model", args.model, "--language", args.language, "--task", args.task]
    return run_legacy("convert_voice_to_article.py", forwarded, args.json)


def cmd_prepare(args: argparse.Namespace) -> int:
    output = args.output.expanduser().resolve(); video = args.video.expanduser().resolve()
    transcript = (args.transcript or video.with_name("transcript.srt")).expanduser().resolve()
    params = {"interval": args.interval, "scene_threshold": args.scene_threshold, "max_candidates": args.max_candidates, "ocr": not args.no_ocr,
              "review": str(args.review_json.resolve()) if args.review_json else None}
    inputs = [video, transcript] + ([args.review_json.expanduser().resolve()] if args.review_json else [])
    stage_fingerprint = fingerprint(inputs, params)
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        old = read_json(manifest_path); prior = old.get("stages", {}).get("candidates_ready", {})
        current = validate(output)
        required_gate = "evidence_selected" if args.review_json else "candidates_ready"
        if prior.get("input_fingerprint") == stage_fingerprint and required_gate in current.passed:
            emit({"ok": True, "cache_hit": True, "package": str(output), "validation": current.to_dict()}, args.json); return 0
    started = time.monotonic()
    forwarded = [str(video), "--transcript", str(transcript), "--output-dir", str(output), "--interval", str(args.interval), "--scene-threshold", str(args.scene_threshold), "--max-candidates", str(args.max_candidates)]
    if args.review_json: forwarded += ["--review-json", str(args.review_json)]
    if args.no_ocr: forwarded.append("--no-ocr")
    code = run_legacy("prepare_learning_package.py", forwarded, False)
    if code: return code
    output.mkdir(parents=True, exist_ok=True)
    metadata = output / "source" / "metadata.json"
    source_video = output / "source" / "source.mp4"
    source_srt = output / "source" / "transcript.srt"
    (output / "source").mkdir(exist_ok=True)
    if video.resolve() != source_video.resolve() and not source_video.exists(): shutil.copy2(video, source_video)
    if transcript.resolve() != source_srt.resolve() and not source_srt.exists(): shutil.copy2(transcript, source_srt)
    if not metadata.exists(): atomic_write_json(metadata, {"platform": "local", "identity": fingerprint([source_video]), "source": video.name})
    existing = read_json(manifest_path) if manifest_path.exists() else {}
    existing.update({"schema_version": SCHEMA_VERSION, "platform": existing.get("platform", "local"), "identity": existing.get("identity", fingerprint([source_video])), "title": existing.get("title", video.stem),
                     "artifacts": {**existing.get("artifacts", {}), "video": "source/source.mp4", "metadata": "source/metadata.json", "transcript_srt": "source/transcript.srt", "candidate_json": "review/keyframes.json", "contact_sheet": "frames/contact_sheet.jpg", "approved_json": "review/approved_keyframes.json", "notes_input": "notes/notes_input.md", "notes": "notes/notes.md"}})
    atomic_write_json(manifest_path, existing)
    checked = validate(output)
    if "candidates_ready" not in checked.passed:
        emit({"ok": False, "validation": checked.to_dict()}, args.json); return 1
    existing = read_json(manifest_path); existing.setdefault("stages", {})["candidates_ready"] = {"duration_ms": round((time.monotonic()-started)*1000), "input_fingerprint": stage_fingerprint, "parameters": params, "cache_hit": False, "result": "passed"}; existing["verified_gate"] = checked.observed_gate
    atomic_write_json(manifest_path, existing)
    emit({"ok": True, "cache_hit": False, "package": str(output), "validation": checked.to_dict()}, args.json); return 0


def cmd_playlist(args: argparse.Namespace) -> int:
    if args.library_root is None or args.output_root is None:
        workspace = discover_workspace(getattr(args, "workspace", None))
        args.library_root = args.library_root or workspace.media
        args.output_root = args.output_root or workspace.media / "playback"
    if args.playlist_action == "verify":
        result = verify_playback(args.output_root)
        emit(result, args.json)
        return 0 if result["ok"] else 1
    if not args.catalog:
        raise ValueError("playlist build requires --catalog")
    if args.source_root is None:
        result = build_playback_views(args.catalog, args.library_root, args.output_root, args.section)
        emit(result, args.json)
        return 0 if result["ok"] else 1
    result = build_xiaoe_playback_views(args.catalog, args.source_root, args.library_root, args.output_root, args.section)
    emit(result, args.json)
    return 0 if result["ok"] else 1


def cmd_migrate_legacy(args: argparse.Namespace) -> int:
    from .migration import build_plan, consolidate_plans, execute_plan, write_plan
    if args.mode == "plan":
        if args.source_root is None: raise ValueError("plan mode requires --source-root")
        result = build_plan(args.source_root, args.library_root, root_media_only=args.root_media_only)
        write_plan(args.report, result)
    elif args.mode == "consolidate":
        result = consolidate_plans([read_json(path) for path in args.input_report])
        write_plan(args.report, result)
    else:
        result = execute_plan(read_json(args.report), args.journal)
    emit(result, args.json)
    return 0 if result.get("ok", False) else 1


def cmd_export_obsidian(args: argparse.Namespace) -> int:
    from .obsidian_export import export_all_verified, export_package
    if args.library_root is None or args.root is None:
        workspace = discover_workspace(getattr(args, "workspace", None))
        args.library_root = args.library_root or workspace.media
        args.root = args.root or workspace.generated
    result = export_all_verified(args.library_root, args.root) if args.all_verified else export_package(args.package, args.root)
    emit(result, args.json)
    return 0 if result.get("ok", False) else 1


def cmd_library(args: argparse.Namespace) -> int:
    from .library import library_status, rebuild_library, search_library, update_package
    if args.library_root is None:
        args.library_root = discover_workspace(getattr(args, "workspace", None)).media
    if args.library_action == "rebuild": result = rebuild_library(args.library_root)
    elif args.library_action == "update": result = update_package(args.package, args.library_root)
    elif args.library_action == "search": result = search_library(args.query, args.library_root, args.limit, args.platform, args.collection, args.section, args.status)
    else: result = library_status(args.library_root)
    emit(result, args.json)
    return 0 if result.get("ok", False) else 1


def cmd_workspace(args: argparse.Namespace) -> int:
    from .command_response import exit_code, response
    from .workspace import WorkspaceError
    try:
        config = discover_workspace(args.workspace)
    except WorkspaceError as exc:
        if args.workspace_action != "doctor":
            raise
        result = response(status="missing_input", workspace=str(args.workspace) if args.workspace else None,
                          validation={"workspace": "failed"}, diagnostics=[str(exc)],
                          next_action={"type": "user", "reason": "provide a valid workspace config"})
        emit(result, args.json)
        return exit_code(result)
    if args.workspace_action == "doctor" and not config.workspace_id:
        result = response(status="missing_input", workspace=str(config.config_path),
                          validation={"workspace_id": "failed"},
                          diagnostics=["workspace_id is required for stable public execution; add a persistent logical ID to workspace.toml"],
                          next_action={"type": "user", "reason": "add workspace_id to workspace.toml; do not change it when moving the workspace"})
        emit(result, args.json)
        return exit_code(result)
    if args.workspace_action == "show":
        if config.schema_version == 2:
            from .command_response import response
            result = response(status="completed", workspace=str(config.config_path), result=config.as_dict(),
                              validation={"workspace_schema": "passed"},
                              provenance={"workspace_config": str(config.config_path)})
        else:
            result = config.as_dict()
    elif args.workspace_action == "doctor":
        if config.schema_version == 2:
            from .source_registry import audit
            try:
                store = audit(config)
                result = response(status="completed", workspace=str(config.config_path),
                                  result={"workspace": config.as_dict(), "snapshot": store},
                                  validation={"workspace_schema": "passed", "snapshot": "passed",
                                              "objects": "passed"},
                                  provenance={"workspace_config": str(config.config_path),
                                              "commit_id": store["commit_id"]})
            except (OSError, ValueError, WorkspaceError) as exc:
                result = response(status="recoverable_failure", workspace=str(config.config_path),
                                  result={"workspace": config.as_dict()},
                                  validation={"workspace_schema": "passed", "snapshot": "failed",
                                              "objects": "not_checked"},
                                  provenance={"workspace_config": str(config.config_path)},
                                  diagnostics=[str(exc)],
                                  next_action={"command": "video-extract workspace doctor --json"})
        else:
            from .capabilities import check_capabilities
            from .installation import inspect
            doctor_result = workspace_doctor(config)
            capabilities = check_capabilities()
            installation = inspect(args.agents_root.expanduser().resolve(), args.codex_root.expanduser().resolve())
            doctor_result["capabilities"] = capabilities
            doctor_result["installation"] = installation
            ok = doctor_result["ok"] and capabilities["status"] == "completed" and installation["status"] == "completed"
            result = response(status="completed" if ok else "recoverable_failure",
                              workspace=str(config.config_path), result=doctor_result,
                              validation={"workspace": doctor_result["ok"],
                                          "capabilities": capabilities["status"] == "completed",
                                          "installation": installation["status"] == "completed"},
                              provenance={"workspace_config": str(config.config_path)},
                              next_action=None if ok else {"command": "video-extract install plan --json"},
                              diagnostics=[] if ok else ["workspace, capability, or installation diagnostics require attention"])
    elif args.workspace_action == "rebuild": result = workspace_rebuild(config, args.apply)
    else: result = prepare_migration(config, args.full_hash)
    emit(result, args.json)
    if args.workspace_action == "doctor":
        return exit_code(result)
    return 0 if result.get("ok", False) or result.get("status") == "completed" else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="video-extract", description="Stable deterministic interface for authorized video-learning packages")
    commands = root.add_subparsers(dest="command", required=True)
    workspace = commands.add_parser("workspace", help="inspect and rebuild a portable workspace")
    workspace_actions = workspace.add_subparsers(dest="workspace_action", required=True)
    for action in ("show", "doctor"):
        p = workspace_actions.add_parser(action); p.add_argument("--workspace", type=Path)
        if action == "doctor":
            p.add_argument("--agents-root", type=Path, default=Path.home() / ".agents")
            p.add_argument("--codex-root", type=Path, default=Path.home() / ".codex")
        p.add_argument("--json", action="store_true"); p.set_defaults(func=cmd_workspace)
    rebuild = workspace_actions.add_parser("rebuild"); rebuild.add_argument("--workspace", type=Path); rebuild.add_argument("--json", action="store_true")
    rebuild_mode = rebuild.add_mutually_exclusive_group(required=True); rebuild_mode.add_argument("--dry-run", action="store_true"); rebuild_mode.add_argument("--apply", action="store_true"); rebuild.set_defaults(func=cmd_workspace)
    migration = workspace_actions.add_parser("prepare-migration"); migration.add_argument("--workspace", type=Path); migration.add_argument("--full-hash", action="store_true"); migration.add_argument("--json", action="store_true"); migration.set_defaults(func=cmd_workspace)
    doctor = commands.add_parser("doctor", help="check local deterministic dependencies"); doctor.add_argument("--json", action="store_true"); doctor.set_defaults(func=cmd_doctor)
    playlist = commands.add_parser("playlist", help="build or verify video-only playback directories and M3U8 playlists")
    playlist.add_argument("playlist_action", nargs="?", choices=("build", "verify"), default="build")
    playlist.add_argument("--catalog", type=Path)
    playlist.add_argument("--source-root", type=Path, help="optional legacy course directory used while packages are being migrated")
    playlist.add_argument("--library-root", type=Path)
    playlist.add_argument("--output-root", type=Path)
    playlist.add_argument("--workspace", type=Path)
    playlist.add_argument("--section", action="append", help="exact section title; omit to build every section")
    playlist.add_argument("--json", action="store_true")
    playlist.set_defaults(func=cmd_playlist)
    migrate = commands.add_parser("migrate-legacy", help="plan or execute deterministic same-device legacy migration")
    migrate.add_argument("--source-root", type=Path); migrate.add_argument("--library-root", type=Path, required=True)
    migrate.add_argument("--mode", choices=("plan", "move", "consolidate"), required=True); migrate.add_argument("--report", type=Path, required=True)
    migrate.add_argument("--input-report", type=Path, action="append", default=[])
    migrate.add_argument("--root-media-only", action="store_true")
    migrate.add_argument("--journal", type=Path, required=True); migrate.add_argument("--json", action="store_true"); migrate.set_defaults(func=cmd_migrate_legacy)
    export = commands.add_parser("export-obsidian", help="export verified reading notes into Obsidian")
    export.add_argument("package", nargs="?", type=Path); export.add_argument("--all-verified", action="store_true")
    export.add_argument("--library-root", type=Path); export.add_argument("--root", type=Path); export.add_argument("--workspace", type=Path)
    export.add_argument("--json", action="store_true"); export.set_defaults(func=cmd_export_obsidian)
    library = commands.add_parser("library", help="manage the rebuildable SQLite FTS5 index")
    library_actions = library.add_subparsers(dest="library_action", required=True)
    for action in ("rebuild", "status"):
        p = library_actions.add_parser(action); p.add_argument("--library-root", type=Path); p.add_argument("--workspace", type=Path); p.add_argument("--json", action="store_true"); p.set_defaults(func=cmd_library)
    update = library_actions.add_parser("update"); update.add_argument("package", type=Path); update.add_argument("--library-root", type=Path); update.add_argument("--workspace", type=Path); update.add_argument("--json", action="store_true"); update.set_defaults(func=cmd_library)
    search = library_actions.add_parser("search"); search.add_argument("query"); search.add_argument("--library-root", type=Path); search.add_argument("--workspace", type=Path); search.add_argument("--limit", type=int, default=10)
    for name in ("platform", "collection", "section", "status"): search.add_argument(f"--{name}")
    search.add_argument("--json", action="store_true"); search.set_defaults(func=cmd_library)
    for name, func in (("verify", cmd_verify), ("status", cmd_status)):
        p = commands.add_parser(name, help=f"{name} package state from actual artifacts"); p.add_argument("path", type=Path); p.add_argument("--catalog", type=Path); p.add_argument("--json", action="store_true"); p.set_defaults(func=func)
        if name == "verify": p.add_argument("--goal", action="append", choices=("podcast_zh", "notes_zh"))
    def media_arguments(p: argparse.ArgumentParser) -> None:
        p.add_argument("--media", choices=("video", "audio", "subtitles", "all"), default="all")
        p.add_argument("--language", default="original")
        p.add_argument("--quality", choices=("standard", "balanced", "high"), default="high")
        p.add_argument("--workspace", type=Path)
        p.add_argument("--json", action="store_true")
    planning = commands.add_parser("plan", help="read-only media extraction plan"); planning.add_argument("source", nargs="?"); planning.add_argument("--request", type=Path, help="versioned scoped media request JSON"); planning.add_argument("--output", type=Path, help="existing package whose validated artifacts may be reused"); media_arguments(planning); planning.set_defaults(func=cmd_plan)
    ensuring = commands.add_parser("ensure", help="materialize requested media into a managed package"); ensuring.add_argument("source", nargs="?"); ensuring.add_argument("--request", type=Path, help="versioned scoped media request JSON"); ensuring.add_argument("--output", type=Path); media_arguments(ensuring); ensuring.set_defaults(func=cmd_ensure)
    operation = commands.add_parser("operation", help="inspect or resume a persistent operation")
    operation_actions = operation.add_subparsers(dest="operation_action", required=True)
    for action in ("show", "resume"):
        p = operation_actions.add_parser(action); p.add_argument("operation_id"); p.add_argument("--workspace", type=Path, required=True); p.add_argument("--json", action="store_true"); p.set_defaults(func=cmd_operation)
    source = commands.add_parser("source", help="import local source material into a managed package")
    source_actions = source.add_subparsers(dest="source_action", required=True)
    source_import = source_actions.add_parser("import"); source_import.add_argument("input", type=Path); source_import.add_argument("--package", type=Path); source_import.add_argument("--title"); source_import.add_argument("--source-id"); source_import.add_argument("--expected-revision", type=int); source_import.add_argument("--workspace", type=Path); source_import.add_argument("--json", action="store_true"); source_import.set_defaults(func=cmd_source)
    source_register = source_actions.add_parser("register", help="register a document or in-place source tree")
    source_register.add_argument("input", type=Path); source_register.add_argument("--title"); source_register.add_argument("--source-id")
    source_register.add_argument("--expected-revision", type=int); source_register.add_argument("--workspace", type=Path)
    source_register.add_argument("--json", action="store_true"); source_register.set_defaults(func=cmd_source)
    source_verify = source_actions.add_parser("verify", help="verify a registered source version")
    source_verify.add_argument("source_id"); source_verify.add_argument("--source-version")
    source_verify.add_argument("--workspace", type=Path); source_verify.add_argument("--json", action="store_true")
    source_verify.set_defaults(func=cmd_source)
    source_relocate = source_actions.add_parser("relocate", help="update a source location after content verification")
    source_relocate.add_argument("source_id"); source_relocate.add_argument("location", type=Path)
    source_relocate.add_argument("--expected-revision", type=int, required=True)
    source_relocate.add_argument("--workspace", type=Path); source_relocate.add_argument("--json", action="store_true")
    source_relocate.set_defaults(func=cmd_source)
    source_reconcile = source_actions.add_parser("reconcile", help="complete durability checks after an uncertain publish")
    source_reconcile.add_argument("source_id"); source_reconcile.add_argument("--source-version", required=True)
    source_reconcile.add_argument("--workspace", type=Path); source_reconcile.add_argument("--json", action="store_true")
    source_reconcile.set_defaults(func=cmd_source)
    notes = commands.add_parser("notes", help="prepare or finalize source-grounded notes")
    notes_actions = notes.add_subparsers(dest="notes_action", required=True)
    for action in ("prepare", "finalize"):
        p = notes_actions.add_parser(action); p.add_argument("package", type=Path); p.add_argument("--workspace", type=Path); p.add_argument("--json", action="store_true"); p.set_defaults(func=cmd_notes)
    install = commands.add_parser("install", help="plan, apply, or check project-owned host integrations")
    install_actions = install.add_subparsers(dest="install_action", required=True)
    for action in ("plan", "apply", "check"):
        p = install_actions.add_parser(action)
        p.add_argument("--agents-root", type=Path, default=Path.home() / ".agents")
        p.add_argument("--codex-root", type=Path, default=Path.home() / ".codex")
        if action == "apply": p.add_argument("--backup-root", type=Path)
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=cmd_install)
    capability = commands.add_parser("capability", help="list, check, or run a stable capability ID")
    capability_actions = capability.add_subparsers(dest="capability_action", required=True)
    capability_list = capability_actions.add_parser("list", help="list declared capability contracts")
    capability_list.add_argument("--json", action="store_true"); capability_list.set_defaults(func=cmd_capability)
    capability_check = capability_actions.add_parser("check", help="check a capability implementation and contract")
    capability_check.add_argument("capability_id", nargs="?"); capability_check.add_argument("--json", action="store_true"); capability_check.set_defaults(func=cmd_capability)
    capability_run = capability_actions.add_parser("run", help="run a capability from a versioned JSON request")
    capability_run.add_argument("capability_id"); capability_run.add_argument("--request", type=Path, required=True)
    capability_run.add_argument("--json", action="store_true"); capability_run.set_defaults(func=cmd_capability)
    scan = commands.add_parser("scan", help="scan a homogeneous collection inventory"); scan.add_argument("source"); scan.add_argument("--platform", choices=("xiaoe", "bilibili", "youtube"), required=True); scan.add_argument("--output", type=Path, required=True); scan.add_argument("--visible", action="store_true"); scan.add_argument("--wait-seconds", type=int); scan.add_argument("--json", action="store_true"); scan.set_defaults(func=cmd_scan)
    acquire = commands.add_parser("acquire", help="acquire authorized media for one platform scope"); acquire.add_argument("source", nargs="?", default=""); acquire.add_argument("--platform", choices=("xiaoe", "bilibili", "youtube"), required=True); acquire.add_argument("--urls-file", type=Path); acquire.add_argument("--output", type=Path, required=True); acquire.add_argument("--media", choices=("video", "audio", "both"), default="video"); acquire.add_argument("--quality", type=int); acquire.add_argument("--workers", type=int); acquire.add_argument("--session-dir", type=Path, default=Path("work/browser_session")); acquire.add_argument("--wait-seconds", type=int); acquire.add_argument("--headless", action="store_true"); acquire.add_argument("--cdp-url", help="连接已打开的 Chrome CDP 地址"); acquire.add_argument("--json", action="store_true"); acquire.set_defaults(func=cmd_acquire)
    xiaoe = commands.add_parser("xiaoe", help="persistent, resumable Xiaoe course downloads")
    xiaoe_actions = xiaoe.add_subparsers(dest="xiaoe_action", required=True)
    xiaoe_login = xiaoe_actions.add_parser("login", help="authorize the persistent Xiaoe download profile once")
    xiaoe_login.add_argument("course_url"); xiaoe_login.add_argument("--wait-seconds", type=int, default=300); xiaoe_login.add_argument("--workspace", type=Path); xiaoe_login.add_argument("--json", action="store_true"); xiaoe_login.set_defaults(func=cmd_xiaoe)
    xiaoe_download = xiaoe_actions.add_parser("download", help="download selected course chapters with resume")
    xiaoe_download.add_argument("course_url"); xiaoe_download.add_argument("--chapters", required=True, help="comma-separated numbers or ranges, e.g. 17,18,19 or 17-19"); xiaoe_download.add_argument("--wait-seconds", type=int, default=12); xiaoe_download.add_argument("--visible", action="store_true"); xiaoe_download.add_argument("--workspace", type=Path); xiaoe_download.add_argument("--json", action="store_true"); xiaoe_download.set_defaults(func=cmd_xiaoe)
    xiaoe_status = xiaoe_actions.add_parser("status", help="show resumable Xiaoe download status")
    xiaoe_status.add_argument("course_url"); xiaoe_status.add_argument("--workspace", type=Path); xiaoe_status.add_argument("--json", action="store_true"); xiaoe_status.set_defaults(func=cmd_xiaoe)
    transcribe = commands.add_parser("transcribe", help="reuse formal subtitles or transcribe media"); transcribe.add_argument("--video", type=Path); transcribe.add_argument("--catalog", type=Path); transcribe.add_argument("--output", type=Path, required=True); transcribe.add_argument("--section"); transcribe.add_argument("--limit", type=int); transcribe.add_argument("--workers", type=int); transcribe.add_argument("--model", default="small"); transcribe.add_argument("--language", default="zh"); transcribe.add_argument("--task", choices=("transcribe", "translate"), default="transcribe"); transcribe.add_argument("--visible", action="store_true"); transcribe.add_argument("--json", action="store_true"); transcribe.set_defaults(func=cmd_transcribe)
    prep = commands.add_parser("prepare-evidence", help="prepare reusable low-resolution evidence candidates"); prep.add_argument("--video", type=Path, required=True); prep.add_argument("--transcript", type=Path); prep.add_argument("--output", type=Path, required=True); prep.add_argument("--review-json", type=Path); prep.add_argument("--interval", type=float, default=15); prep.add_argument("--scene-threshold", type=float, default=.30); prep.add_argument("--max-candidates", type=int, default=40); prep.add_argument("--no-ocr", action="store_true"); prep.add_argument("--json", action="store_true"); prep.set_defaults(func=cmd_prepare)
    return root


def main() -> int:
    args = parser().parse_args()
    try: return args.func(args)
    except KeyboardInterrupt: return 130
    except Exception as exc:
        emit({"ok": False, "error": str(exc)}, getattr(args, "json", False)); return 1


if __name__ == "__main__": raise SystemExit(main())
