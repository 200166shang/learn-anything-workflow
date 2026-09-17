"""Rebuildable, generation-pinned Obsidian projection of learning records."""

from __future__ import annotations

import hashlib
import html
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .command_response import response
from .learning import _load as load_learning
from .manifest import atomic_write_json, read_json
from .workspace import PORTABLE_SCHEMA_VERSION, WorkspaceConfig, WorkspaceError

SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "schemas" /
                     "learning-view-generation-v1.schema.json").read_text(encoding="utf-8"))


def _roots(config: WorkspaceConfig) -> tuple[Path, Path, Path, Path]:
    if config.schema_version != PORTABLE_SCHEMA_VERSION or config.derived is None or config.results is None:
        raise WorkspaceError("learning views require workspace schema v2")
    root = config.derived / "learning-views"
    if root.is_symlink():
        raise WorkspaceError(f"refusing symbolic learning view root: {root}")
    return root, root / "generations", root / "current", root / "status"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _sync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_generation(path: Path) -> None:
    for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
        with file_path.open("rb") as stream:
            os.fsync(stream.fileno())
    for directory in sorted((item for item in path.rglob("*") if item.is_dir()), reverse=True):
        _sync_path(directory)
    _sync_path(path)


def _latest_feedback(record: dict[str, Any], question_id: str) -> str | None:
    values = [item for item in record["feedbacks"].values() if item["question_id"] == question_id]
    return max(values, key=lambda item: item["created_at"])["state"] if values else None


def _document(config: WorkspaceConfig, refs: list[dict[str, Any]], target: Path) -> dict[str, Any]:
    ref = refs[0]
    if any(item["logical_path"] != ref["logical_path"] or item["object_sha256"] != ref["object_sha256"]
           for item in refs):
        raise WorkspaceError(f"explanation revision has inconsistent objects: {target.name}")
    source = config.results / ref["logical_path"]
    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != ref["object_sha256"]:
        raise WorkspaceError(f"explanation object missing or corrupt: {ref['object_sha256']}")
    text = source.read_text(encoding="utf-8")
    rendered = text
    for section_id in dict.fromkeys(item["section_id"] for item in refs):
        marker = f"<!-- section-id: {section_id} -->"
        if marker not in rendered:
            raise WorkspaceError(f"explanation locator is invalid: {section_id}")
        rendered = rendered.replace(marker, marker + f"\n^{section_id}", 1)
    target.write_text(rendered, encoding="utf-8")
    return {"logical_path": str(target), "object_sha256": ref["object_sha256"],
            "projection_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def _render_html(module: dict[str, Any], thread: dict[str, Any] | None,
                 nodes: dict[str, Any], edges: list[dict[str, Any]]) -> str:
    current = nodes.get(thread["current_question_id"]) if thread else None
    route = " → ".join(nodes.get(frame["question_id"], {}).get("title", "跨模块")
                       for frame in (thread or {}).get("return_route", []))
    cards = []
    for node in nodes.values():
        classes = "node current" if node["is_current"] else "node"
        state = {"understood": "理解了", "confused": "仍不理解", "parked": "暂放", None: "未确认"}[node["feedback"]]
        content = {"available": "完整讲解", "pending": "待讲解", "broken": "定位待修复"}[node["content_state"]]
        link = (f'<a href="{html.escape(node["href"])}">{html.escape(node["title"])}</a>'
                if node.get("href") else f'<span>{html.escape(node["title"])}</span>')
        boundary = " · 跨根引用" if node.get("cross_root") else ""
        cards.append(f'<article class="{classes}" data-question="{node["question_id"]}">{link}'
                     f'<small>{state} · {content}{boundary}</small></article>')
    edge_lines = "".join(f'<li class="{e["kind"]}">{html.escape(nodes[e["from_question_id"]]["title"])} '
                         f'→ {html.escape(nodes[e["to_question_id"]]["title"])}</li>' for e in edges)
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(module["goal"])} · 局部问题图</title><style>
:root{{--bg:#f7f8fa;--paper:#fff;--ink:#20242b;--muted:#697386;--blue:#3485ff;--line:#cbd3df}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,sans-serif}}header{{padding:20px 24px 12px}}h1{{margin:0 0 8px;font-size:22px}}.route{{color:var(--muted)}}.graph{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:18px;padding:20px 24px}}.node{{border:1px solid var(--line);border-radius:14px;background:var(--paper);padding:16px;box-shadow:0 4px 18px #172b4d12}}.node.current{{outline:3px solid #3485ff55;border-color:var(--blue)}}a{{color:var(--ink);font-weight:700;text-decoration:none}}small{{display:block;color:var(--muted);margin-top:8px}}.legend,ul{{margin:0 24px 20px;padding:14px 20px;background:var(--paper);border-radius:12px}}li.reference{{border-left:3px dashed #a25ad6;padding-left:8px}}@media(max-width:520px){{header,.graph{{padding-left:12px;padding-right:12px}}}}
</style></head><body><header><h1>{html.escape(module["goal"])}</h1><div>续学位置：<strong>{html.escape(current["title"] if current else "尚未开始")}</strong> · {"暂放" if current and current["feedback"] == "parked" else "进行中"}</div><div class="route">本次返回路线：{html.escape(route or "当前线程")}</div></header><main class="graph">{''.join(cards)}</main><ul>{edge_lines}</ul><p class="legend">实线：实际追问 · 虚线：跨根引用 · 点击仅查看，不改学习状态 · 跨根引用保持边界 · 顶部暂放入口随反馈显示</p></body></html>'''


def _generation_valid(generation: Path, module_id: str) -> bool:
    try:
        manifest = read_json(generation / "manifest.json")
        Draft202012Validator(SCHEMA).validate(manifest)
        if manifest["module_id"] != module_id or manifest["generation_id"] != generation.name:
            return False
        graph = generation / "局部问题图.html"
        if (not graph.is_file()
                or hashlib.sha256(graph.read_bytes()).hexdigest() != manifest["view_sha256"]):
            return False
        for document in manifest["documents"].values():
            path = generation / document["logical_path"]
            if (not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != document["projection_sha256"]
                    or f'^{document["section_id"]}' not in path.read_text(encoding="utf-8")):
                return False
        return True
    except (OSError, KeyError, TypeError, ValueError, ValidationError):
        return False


def build_view(config: WorkspaceConfig, module_id: str) -> dict[str, Any]:
    root, generations, current_root, status_root = _roots(config)
    snapshot = load_learning(config); record = snapshot["record"]
    module = record["modules"].get(module_id)
    if module is None:
        return response(status="missing_input", workspace=str(config.config_path), diagnostics=[f"unknown module_id: {module_id}"])
    threads = [record["threads"][item] for item in module["thread_ids"]]
    owned = {question_id: question for question_id, question in record["questions"].items()
             if question["thread_id"] in module["thread_ids"]}
    visible = dict(owned)
    for relationship in record["relationships"].values():
        if relationship["type"] == "reference" and relationship["from_question_id"] in owned:
            target = record["questions"].get(relationship["to_question_id"])
            if target is not None:
                visible[target["question_id"]] = target
    thread = record["threads"].get(module.get("last_active_thread_id") or "")
    root_digest = _digest({"module": module, "threads": threads, "questions": owned,
                           "relationships": record["relationships"], "feedbacks": record["feedbacks"]})
    generation_seed = {"learning_commit_id": snapshot["commit_id"], "module_id": module_id,
                       "root_digest": root_digest}
    generation_id = "view-generation-" + _digest(generation_seed)
    root.mkdir(parents=True, exist_ok=True); generations.mkdir(exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".building-", dir=generations))
    try:
        docs = temp / "完整讲解"; docs.mkdir()
        nodes: dict[str, Any] = {}; documents: dict[str, Any] = {}
        latest_refs = {question_id: question["explanation_refs"][-1]
                       for question_id, question in visible.items() if question["explanation_refs"]}
        grouped_refs: dict[str, list[dict[str, Any]]] = {}
        for ref in latest_refs.values():
            filename = f'{ref["explanation_id"]}-r{ref["explanation_revision"]}.md'
            grouped_refs.setdefault(filename, []).append(ref)
        projected: dict[str, dict[str, Any] | None] = {}
        for filename, refs in grouped_refs.items():
            try:
                projected[filename] = _document(config, refs, docs / filename)
            except WorkspaceError:
                projected[filename] = None
        for question_id, question in visible.items():
            refs = question["explanation_refs"]
            content_state, href = "pending", None
            if refs:
                ref = refs[-1]; filename = f'{ref["explanation_id"]}-r{ref["explanation_revision"]}.md'
                locator = projected[filename]
                if locator is not None:
                    content_state = "available"
                    href = f'完整讲解/{filename}#^{ref["section_id"]}'
                    documents[question_id] = {**locator, "section_id": ref["section_id"],
                                              "logical_path": f"完整讲解/{filename}"}
                else:
                    content_state = "broken"
            nodes[question_id] = {"question_id": question_id, "title": question["title"],
                                  "feedback": _latest_feedback(record, question_id),
                                  "content_state": content_state, "href": href,
                                  "is_current": bool(thread and question_id == thread["current_question_id"]),
                                  "cross_root": question_id not in owned,
                                  "unresolved_confusions": question["unresolved_confusions"]}
        edges = [{"from_question_id": item["from_question_id"], "to_question_id": item["to_question_id"],
                  "kind": item["type"], "cross_root": item["type"] == "reference"}
                 for item in record["relationships"].values()
                 if item["from_question_id"] in nodes and item["to_question_id"] in nodes]
        graph = temp / "局部问题图.html"
        graph.write_text(_render_html(module, thread, nodes, edges), encoding="utf-8")
        manifest = {"schema_version": 1, "generation_id": generation_id, "module_id": module_id,
                    "learning_commit_id": snapshot["commit_id"], "root_digest": root_digest,
                    "view_sha256": hashlib.sha256(graph.read_bytes()).hexdigest(),
                    "current_question_id": thread["current_question_id"] if thread else None,
                    "nodes": nodes, "edges": edges, "documents": documents}
        Draft202012Validator(SCHEMA).validate(manifest)
        (temp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _sync_generation(temp)
        destination = generations / generation_id
        if destination.exists():
            shutil.rmtree(temp)
            if not _generation_valid(destination, module_id):
                raise WorkspaceError("existing immutable view generation was modified or corrupted; preserving it for inspection")
        else:
            os.replace(temp, destination)
            _sync_path(generations)
        if os.environ.get("VIDEO_EXTRACT_VIEW_TEST_FAULT") == "before_publish":
            raise OSError("injected failure before view publish")
        current_root.mkdir(exist_ok=True)
        pointer = current_root / f"{module_id}.json"
        atomic_write_json(pointer, {"schema_version": 1, "module_id": module_id,
                                    "generation_id": generation_id, "learning_commit_id": snapshot["commit_id"]})
        status_root.mkdir(exist_ok=True)
        atomic_write_json(status_root / f"{module_id}.json", {"sync_state": "current", "learning_commit_id": snapshot["commit_id"]})
        return response(status="completed", workspace=str(config.config_path), result={
            "generation_id": generation_id, "generation_path": str(destination),
            "manifest_path": str(destination / "manifest.json"), "pointer_path": str(pointer),
            "learning_commit_id": snapshot["commit_id"]}, validation={"projection": "passed"})
    except Exception as exc:
        if temp.exists(): shutil.rmtree(temp)
        status_root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(status_root / f"{module_id}.json", {"sync_state": "unsynced",
                          "learning_commit_id": snapshot["commit_id"], "diagnostic": str(exc)})
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        diagnostics=[str(exc)], validation={"projection": "unsynced"},
                        next_action={"type": "retry", "command": f"video-extract view build --module-id {module_id}"})


def status_view(config: WorkspaceConfig, module_id: str) -> dict[str, Any]:
    _, generations, current_root, status_root = _roots(config)
    snapshot = load_learning(config); pointer = current_root / f"{module_id}.json"
    served = read_json(pointer) if pointer.is_file() else None
    generation_ok = bool(served and _generation_valid(generations / served["generation_id"], module_id))
    sync = "current" if generation_ok and served["learning_commit_id"] == snapshot["commit_id"] else "unsynced"
    attempt = read_json(status_root / f"{module_id}.json") if (status_root / f"{module_id}.json").is_file() else None
    return response(status="completed", workspace=str(config.config_path), result={
        "module_id": module_id, "sync_state": sync, "learning_commit_id": snapshot["commit_id"],
        "served_generation": served.get("generation_id") if served else None,
        "served_learning_commit_id": served.get("learning_commit_id") if served else None,
        "last_attempt": attempt}, validation={"projection": sync})


def locate_view(config: WorkspaceConfig, question_id: str) -> dict[str, Any]:
    snapshot = load_learning(config); question = snapshot["record"]["questions"].get(question_id)
    if question is None:
        return response(status="missing_input", workspace=str(config.config_path), diagnostics=[f"unknown question_id: {question_id}"])
    module_id = snapshot["record"]["threads"][question["thread_id"]]["module_id"]
    _, generations, current_root, _ = _roots(config); pointer = current_root / f"{module_id}.json"
    if not pointer.is_file():
        return response(status="recoverable_failure", workspace=str(config.config_path), result={"content_state": "unsynced"}, diagnostics=["view has not been built"])
    selected = read_json(pointer); generation = generations / selected["generation_id"]
    if not _generation_valid(generation, module_id):
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        result={"question_id": question_id, "content_state": "unsynced"},
                        diagnostics=["served view generation is missing, modified, or corrupt"])
    manifest = read_json(generation / "manifest.json"); node = manifest["nodes"].get(question_id)
    if node is None:
        return response(status="recoverable_failure", workspace=str(config.config_path), result={"content_state": "unsynced"}, diagnostics=["question is absent from served generation"])
    if node["content_state"] == "pending":
        return response(status="awaiting_model", workspace=str(config.config_path), result={"question_id": question_id, "content_state": "pending"}, next_action={"type": "model", "action": "prepare_explanation"})
    if node["content_state"] != "available" or not node.get("href"):
        return response(status="recoverable_failure", workspace=str(config.config_path), result={"question_id": question_id, "content_state": "broken"}, diagnostics=["explanation locator is invalid; refusing to open document start"])
    document = manifest["documents"][question_id]; path = generation / document["logical_path"]
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != document["projection_sha256"]:
        return response(status="recoverable_failure", workspace=str(config.config_path), result={"question_id": question_id, "content_state": "broken"}, diagnostics=["projected explanation is missing or corrupt"])
    return response(status="completed", workspace=str(config.config_path), result={"question_id": question_id,
                    "content_state": "available", "generation_id": selected["generation_id"],
                    "document_path": str(path), "section_id": document["section_id"], "obsidian_href": node["href"]})
