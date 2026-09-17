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
                     "learning-view-generation-v2.schema.json").read_text(encoding="utf-8"))


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


def _document(config: WorkspaceConfig, refs: list[dict[str, Any]], target: Path,
              generation_id: str, learning_commit_id: str) -> dict[str, Any]:
    ref = refs[0]
    if any(item["logical_path"] != ref["logical_path"] or item["object_sha256"] != ref["object_sha256"]
           for item in refs):
        raise WorkspaceError(f"explanation revision has inconsistent objects: {target.name}")
    source = config.results / ref["logical_path"]
    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != ref["object_sha256"]:
        raise WorkspaceError(f"explanation object missing or corrupt: {ref['object_sha256']}")
    text = source.read_text(encoding="utf-8")
    rendered = (f"> 视图代：`{generation_id}` · 学习修订：`{learning_commit_id}`\n\n"
                f"{text}")
    for section_id in dict.fromkeys(item["section_id"] for item in refs):
        marker = f"<!-- section-id: {section_id} -->"
        if marker not in rendered:
            raise WorkspaceError(f"explanation locator is invalid: {section_id}")
        rendered = rendered.replace(marker, marker + f"\n^{section_id}", 1)
    target.write_text(rendered, encoding="utf-8")
    return {"logical_path": str(target), "object_sha256": ref["object_sha256"],
            "projection_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def _render_html(module: dict[str, Any], thread: dict[str, Any] | None,
                 nodes: dict[str, Any], edges: list[dict[str, Any]], generation_id: str,
                 learning_commit_id: str) -> str:
    current = nodes.get(thread["current_question_id"]) if thread else None
    route = " → ".join(nodes.get(frame["question_id"], {}).get("title", "跨模块")
                       for frame in (thread or {}).get("return_route", []))
    parked = [node for node in nodes.values() if node["feedback"] == "parked"]
    parked_links = " · ".join(
        f'<a href="#{html.escape(node["question_id"])}">{html.escape(node["title"])}</a>'
        for node in parked) or "无"
    node_ids = list(nodes)
    depths = {question_id: 0 for question_id in node_ids}
    for _ in node_ids:
        changed = False
        for edge in edges:
            if edge["cross_root"]:
                continue
            proposed = depths[edge["from_question_id"]] + 1
            if proposed > depths[edge["to_question_id"]]:
                depths[edge["to_question_id"]] = proposed; changed = True
        if not changed:
            break
    rows: dict[int, int] = {}
    positions: dict[str, tuple[int, int]] = {}
    for question_id in node_ids:
        depth = depths[question_id]
        row = rows.get(depth, 0); rows[depth] = row + 1
        positions[question_id] = (30 + depth * 270, 30 + row * 120)
    cards = []
    for node in nodes.values():
        classes = "node current" if node["is_current"] else "node"
        state = {"understood": "理解了", "confused": "仍不理解", "parked": "暂放", None: "未确认"}[node["feedback"]]
        content = {"available": "完整讲解", "pending": "待讲解", "broken": "定位待修复"}[node["content_state"]]
        link = (f'<a href="{html.escape(node["href"])}">{html.escape(node["title"])}</a>'
                if node.get("href") else f'<span>{html.escape(node["title"])}</span>')
        boundary = (f' · 跨根引用：{html.escape(node["source_root_title"])}'
                    if node.get("cross_root") else "")
        x, y = positions[node["question_id"]]
        cards.append(f'<foreignObject x="{x}" y="{y}" width="230" height="92">'
                     f'<article xmlns="http://www.w3.org/1999/xhtml" id="{html.escape(node["question_id"])}" class="{classes}" data-question="{html.escape(node["question_id"])}">{link}'
                     f'<small>{state} · {content}{boundary}</small></article></foreignObject>')
    edge_lines = []
    for edge in edges:
        from_x, from_y = positions[edge["from_question_id"]]
        to_x, to_y = positions[edge["to_question_id"]]
        dash = ' stroke-dasharray="8 6"' if edge["cross_root"] else ""
        label = (f'引用·{nodes[edge["to_question_id"]].get("source_root_title") or "跨根"}'
                 if edge["cross_root"] else edge["kind"])
        middle_x = (from_x + 230 + to_x) // 2
        middle_y = (from_y + to_y) // 2 + 40
        edge_lines.append(f'<g class="edge {edge["kind"]}"><line x1="{from_x + 230}" y1="{from_y + 46}" '
                          f'x2="{to_x}" y2="{to_y + 46}"{dash}/><text x="{middle_x}" y="{middle_y}">{html.escape(label)}</text></g>')
    svg_width = max(320, max((x for x, _ in positions.values()), default=30) + 270)
    svg_height = max(150, max((y for _, y in positions.values()), default=30) + 130)
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(module["goal"])} · 局部问题图</title><style>
:root{{--bg:#f7f8fa;--paper:#fff;--ink:#20242b;--muted:#697386;--blue:#3485ff;--line:#cbd3df}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,sans-serif}}header{{padding:20px 24px 12px}}h1{{margin:0 0 8px;font-size:22px}}.route,.generation{{color:var(--muted)}}.parked{{margin-top:6px}}.graph-scroll{{overflow:auto;padding:8px 24px 20px}}.node{{height:88px;border:1px solid var(--line);border-radius:14px;background:var(--paper);padding:14px;box-shadow:0 4px 18px #172b4d12;scroll-margin-top:16px}}.node.current{{outline:3px solid #3485ff55;border-color:var(--blue)}}a{{color:var(--ink);font-weight:700;text-decoration:none}}small{{display:block;color:var(--muted);margin-top:8px}}.relations{{display:block;background:var(--paper);border-radius:12px;min-width:100%}}.relations line{{stroke:#60718a;stroke-width:3}}.relations .reference line{{stroke:#a25ad6}}.relations text{{fill:var(--ink);font-size:12px}}.legend{{margin:0 24px 20px;padding:14px 20px;background:var(--paper);border-radius:12px}}@media(max-width:520px){{header,.graph-scroll{{padding-left:12px;padding-right:12px}}.legend{{margin-left:12px;margin-right:12px}}}}
</style></head><body><header><h1>{html.escape(module["goal"])} · 局部问题图</h1><div>续学位置：<strong>{html.escape(current["title"] if current else "尚未开始")}</strong> · {"暂放" if current and current["feedback"] == "parked" else "进行中"}</div><div class="route">本次返回路线：{html.escape(route or "当前线程")}</div><div class="parked">暂放入口：{parked_links}</div><div class="generation">视图代：{generation_id} · 学习修订：{learning_commit_id}</div></header><main class="graph-scroll"><svg class="relations" width="{svg_width}" height="{svg_height}" viewBox="0 0 {svg_width} {svg_height}" role="img" aria-label="实际追问与跨根引用关系">{''.join(edge_lines)}{''.join(cards)}</svg></main><p class="legend">实线：实际追问 · 虚线：跨根引用 · 点击仅查看，不改学习状态 · 跨根引用保持边界</p></body></html>'''


def _node_link(node: dict[str, Any]) -> str:
    label = html.escape(node["title"])
    return f'<a href="{html.escape(node["href"])}">{label}</a>' if node.get("href") else f"<span>{label}</span>"


def _render_overview(module: dict[str, Any], groups: list[dict[str, Any]], nodes: dict[str, Any],
                     edges: list[dict[str, Any]], generation_id: str, learning_commit_id: str) -> str:
    sections = []
    positions: dict[str, tuple[int, int]] = {}
    for group in groups:
        cards = []
        group_index = len(sections)
        for question_index, question_id in enumerate(group["question_ids"]):
            node = nodes[question_id]
            positions[question_id] = (190 + question_index * 260, 45 + group_index * 150)
            cards.append(f'<li data-question="{html.escape(question_id)}">{_node_link(node)}'
                         f'<small>{node["content_state"]} · {node["feedback"] or "unconfirmed"}</small></li>')
        sections.append(f'<section><h2>{html.escape(group["root_title"])}</h2><ul>{"".join(cards)}</ul></section>')
    external_ids = [question_id for question_id, node in nodes.items() if node["cross_root"]]
    for external_index, question_id in enumerate(external_ids):
        positions[question_id] = (190 + external_index * 260, 45 + len(groups) * 150)
    graph_nodes = []
    for question_id, (x, y) in positions.items():
        node = nodes[question_id]
        graph_nodes.append(
            f'<foreignObject x="{x}" y="{y}" width="220" height="86">'
            f'<article xmlns="http://www.w3.org/1999/xhtml" id="map-{html.escape(question_id)}" '
            f'class="map-node{(" external" if node["cross_root"] else "")}">{_node_link(node)}'
            f'<small>{html.escape(node["root_title"])} · {node["content_state"]}</small>'
            f'</article></foreignObject>')
    graph_edges = []
    for edge in edges:
        if edge["from_question_id"] not in positions or edge["to_question_id"] not in positions:
            continue
        from_x, from_y = positions[edge["from_question_id"]]
        to_x, to_y = positions[edge["to_question_id"]]
        dash = ' stroke-dasharray="8 6"' if edge["cross_root"] else ""
        graph_edges.append(
            f'<line class="map-edge{(" reference" if edge["cross_root"] else "")}" '
            f'x1="{from_x + 220}" y1="{from_y + 42}" x2="{to_x}" y2="{to_y + 42}"{dash}/>')
    graph_width = max(520, max((x for x, _ in positions.values()), default=190) + 250)
    graph_height = max(170, max((y for _, y in positions.values()), default=45) + 120)
    references = [edge for edge in edges if edge["cross_root"]]
    boundary = "".join(f'<li>{html.escape(nodes[edge["from_question_id"]]["title"])} → '
                       f'{html.escape(nodes[edge["to_question_id"]]["title"])}</li>' for edge in references) or "<li>无</li>"
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{html.escape(module["goal"])} · 模块全景图</title><style>
body{{margin:0;padding:24px;background:#f7f8fa;color:#20242b;font:14px/1.5 system-ui,sans-serif}}header{{margin-bottom:18px}}main{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}}section{{background:#fff;border:1px solid #d8dee8;border-radius:14px;padding:16px}}h1,h2{{margin-top:0}}ul{{padding-left:20px}}li{{margin:10px 0}}small{{display:block;color:#697386}}a{{color:#20242b;font-weight:700;text-decoration:none}}.map-scroll{{overflow:auto;margin-bottom:18px;background:#fff;border-radius:14px}}.map-edge{{stroke:#60718a;stroke-width:3}}.map-edge.reference{{stroke:#a25ad6}}.map-node{{height:82px;padding:12px;border:1px solid #b8c1cf;border-radius:12px;background:#fff}}.map-node.external{{border-style:dashed;border-color:#a25ad6}}.boundary{{margin-top:18px}}@media(max-width:520px){{body{{padding:12px}}main{{display:block}}section{{margin-bottom:12px;min-width:250px}}}}
</style></head><body><header><h1>{html.escape(module["goal"])} · 模块全景图</h1><div>按根分组 · 视图代：{generation_id} · 学习修订：{learning_commit_id}</div></header><div class="map-scroll"><svg width="{graph_width}" height="{graph_height}" viewBox="0 0 {graph_width} {graph_height}" role="img" aria-label="按根分组的追问与跨根引用关系">{''.join(graph_edges)}{''.join(graph_nodes)}</svg></div><p>实线：真实追问 · 虚线：跨根引用边界</p><main>{''.join(sections)}</main><section class="boundary"><h2>跨根引用边界</h2><ul>{boundary}</ul></section></body></html>'''


def _render_catalog(module: dict[str, Any], nodes: dict[str, Any], generation_id: str,
                    learning_commit_id: str) -> str:
    rows = []
    for node in sorted(nodes.values(), key=lambda item: (item["root_title"], item["title"], item["question_id"])):
        searchable = " ".join((node["title"], node["root_title"], node["feedback"] or "", node["content_state"]))
        rows.append(f'<tr data-search="{html.escape(searchable.casefold())}"><td>{_node_link(node)}</td>'
                    f'<td>{html.escape(node["root_title"])}</td><td>{html.escape(node["feedback"] or "未确认")}</td>'
                    f'<td>{html.escape(node["content_state"])}</td></tr>')
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{html.escape(module["goal"])} · 问题目录</title><style>
body{{margin:0;padding:24px;background:#f7f8fa;color:#20242b;font:14px/1.5 system-ui,sans-serif}}input{{width:100%;padding:10px 12px;margin:12px 0;border:1px solid #b8c1cf;border-radius:8px}}table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:10px;border-bottom:1px solid #e1e5eb;text-align:left}}a{{color:#20242b;font-weight:700;text-decoration:none}}@media(max-width:520px){{body{{padding:12px}}table{{min-width:650px}}.scroll{{overflow:auto}}}}
</style></head><body><h1>{html.escape(module["goal"])} · 问题目录</h1><div>视图代：{generation_id} · 学习修订：{learning_commit_id}</div><label>搜索问题、根、反馈或内容状态<input id="query" type="search" placeholder="输入关键词"></label><div class="scroll"><table><thead><tr><th>问题</th><th>所属根</th><th>反馈</th><th>内容</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div><script>
const input=document.getElementById('query');input.addEventListener('input',()=>{{const q=input.value.trim().toLocaleLowerCase();document.querySelectorAll('tbody tr').forEach(row=>row.hidden=q&&!row.dataset.search.includes(q));}});
</script></body></html>'''


def _validate_manifest_semantics(manifest: dict[str, Any]) -> None:
    nodes = manifest["nodes"]
    if any(question_id != node["question_id"] for question_id, node in nodes.items()):
        raise ValidationError("node map key must equal question_id")
    if any(question_id not in nodes for question_id in manifest["local_node_ids"]):
        raise ValidationError("local_node_ids must reference nodes")
    grouped = [question_id for group in manifest["root_groups"] for question_id in group["question_ids"]]
    if len(grouped) != len(set(grouped)):
        raise ValidationError("module questions must occur in exactly one root group")
    if any(question_id not in nodes or nodes[question_id]["cross_root"] for question_id in grouped):
        raise ValidationError("root groups may contain only module-owned nodes")
    module_owned = {question_id for question_id, node in nodes.items() if not node["cross_root"]}
    if set(grouped) != module_owned:
        raise ValidationError("root groups must cover every module-owned node")
    for edge in manifest["edges"]:
        if edge["from_question_id"] not in nodes or edge["to_question_id"] not in nodes:
            raise ValidationError("edge endpoints must reference nodes")
        if edge["cross_root"] and not edge.get("reference_id"):
            raise ValidationError("cross-root edge requires reference_id")
    for question_id, document in manifest["documents"].items():
        if question_id not in nodes or nodes[question_id]["content_state"] != "available":
            raise ValidationError("documents must belong to available nodes")
        expected_href = f'{document["logical_path"]}#^{document["section_id"]}'
        if nodes[question_id].get("href") != expected_href:
            raise ValidationError("available node href must match its document locator")
    available = {question_id for question_id, node in nodes.items()
                 if node["content_state"] == "available"}
    if available != set(manifest["documents"]):
        raise ValidationError("every available node must have exactly one document locator")


def _validated_manifest(generation: Path, module_id: str) -> dict[str, Any] | None:
    try:
        manifest = read_json(generation / "manifest.json")
        Draft202012Validator(SCHEMA).validate(manifest)
        _validate_manifest_semantics(manifest)
        if manifest["module_id"] != module_id or manifest["generation_id"] != generation.name:
            return None
        for filename, digest in manifest.get("views", {"局部问题图.html": manifest["view_sha256"]}).items():
            view = generation / filename
            if not view.is_file() or hashlib.sha256(view.read_bytes()).hexdigest() != digest:
                return None
        for document in manifest["documents"].values():
            path = generation / document["logical_path"]
            if (not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != document["projection_sha256"]
                    or f'^{document["section_id"]}' not in path.read_text(encoding="utf-8")):
                return None
        return manifest
    except (OSError, KeyError, TypeError, ValueError, ValidationError):
        return None


def _read_pointer(path: Path, module_id: str) -> dict[str, Any] | None:
    try:
        value = read_json(path)
        generation_id = value.get("generation_id")
        learning_commit_id = value.get("learning_commit_id")
        manifest_sha256 = value.get("manifest_sha256")
        if (value.get("schema_version") != 2 or value.get("module_id") != module_id
                or not isinstance(generation_id, str)
                or len(generation_id) != len("view-generation-") + 64
                or not generation_id.startswith("view-generation-")
                or not isinstance(learning_commit_id, str)
                or len(learning_commit_id) != len("learning-commit-") + 64
                or not learning_commit_id.startswith("learning-commit-")
                or not isinstance(manifest_sha256, str)
                or len(manifest_sha256) != 64):
            return None
        alphabet = frozenset("0123456789abcdef")
        if (not set(generation_id.removeprefix("view-generation-")) <= alphabet
                or not set(learning_commit_id.removeprefix("learning-commit-")) <= alphabet
                or not set(manifest_sha256) <= alphabet):
            return None
        return value
    except (OSError, TypeError, ValueError):
        return None


def build_view(config: WorkspaceConfig, module_id: str) -> dict[str, Any]:
    root, generations, current_root, status_root = _roots(config)
    snapshot = load_learning(config); record = snapshot["record"]
    module = record["modules"].get(module_id)
    if module is None:
        return response(status="missing_input", workspace=str(config.config_path), diagnostics=[f"unknown module_id: {module_id}"])
    module_threads = {thread_id: record["threads"][thread_id] for thread_id in module["thread_ids"]}
    active_thread = module_threads.get(module.get("last_active_thread_id") or "")
    module_questions = {question_id: question for question_id, question in record["questions"].items()
                        if question["thread_id"] in module_threads}
    external_questions: dict[str, dict[str, Any]] = {}
    cross_edges: list[dict[str, Any]] = []
    for question_id, question in module_questions.items():
        for reference in question.get("cross_root_references", []):
            source_id = reference["source"]["question_id"]
            source = record["questions"].get(source_id)
            if source is None:
                continue
            external_questions[source_id] = source
            cross_edges.append({"from_question_id": question_id, "to_question_id": source_id,
                                "kind": "reference", "cross_root": True,
                                "reference_id": reference["reference_id"]})
    all_questions = {**module_questions, **external_questions}
    regular_edges = [{"from_question_id": item["from_question_id"],
                      "to_question_id": item["to_question_id"], "kind": item["type"],
                      "cross_root": False}
                     for item in record["relationships"].values()
                     if item["from_question_id"] in module_questions
                     and item["to_question_id"] in module_questions]
    edges = [*regular_edges, *cross_edges]
    feedbacks = {key: value for key, value in record["feedbacks"].items()
                 if value["question_id"] in all_questions}
    root_digest = _digest({"module": module, "threads": module_threads,
                           "questions": all_questions, "edges": edges, "feedbacks": feedbacks})
    generation_seed = {"learning_commit_id": snapshot["commit_id"], "module_id": module_id,
                       "root_digest": root_digest}
    generation_id = "view-generation-" + _digest(generation_seed)
    root.mkdir(parents=True, exist_ok=True); generations.mkdir(exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".building-", dir=generations))
    try:
        docs = temp / "完整讲解"; docs.mkdir()
        nodes: dict[str, Any] = {}; documents: dict[str, Any] = {}
        latest_refs = {question_id: question["explanation_refs"][-1]
                       for question_id, question in all_questions.items() if question["explanation_refs"]}
        grouped_refs: dict[str, list[dict[str, Any]]] = {}
        for ref in latest_refs.values():
            filename = f'{ref["explanation_id"]}-r{ref["explanation_revision"]}.md'
            grouped_refs.setdefault(filename, []).append(ref)
        projected: dict[str, dict[str, Any] | None] = {}
        for filename, refs in grouped_refs.items():
            try:
                projected[filename] = _document(config, refs, docs / filename, generation_id,
                                                 snapshot["commit_id"])
            except WorkspaceError:
                projected[filename] = None
        root_titles = {thread_id: record["questions"][value["root_question_id"]]["title"]
                       for thread_id, value in record["threads"].items()}
        for question_id, question in all_questions.items():
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
            thread = record["threads"][question["thread_id"]]
            question_module_id = thread["module_id"]
            nodes[question_id] = {"question_id": question_id, "title": question["title"],
                                  "feedback": _latest_feedback(record, question_id),
                                  "content_state": content_state, "href": href,
                                  "is_current": bool(active_thread and question_id == active_thread["current_question_id"]),
                                  "cross_root": question_module_id != module_id,
                                  "source_root_title": (root_titles[question["thread_id"]]
                                                        if question_module_id != module_id else None),
                                  "root_title": root_titles[question["thread_id"]],
                                  "module_id": question_module_id,
                                  "unresolved_confusions": question["unresolved_confusions"]}
        local_ids = ({question_id for question_id, question in module_questions.items()
                      if active_thread and question["thread_id"] == active_thread["thread_id"]})
        local_ids.update(edge["to_question_id"] for edge in cross_edges
                         if edge["from_question_id"] in local_ids)
        local_nodes = {question_id: nodes[question_id] for question_id in local_ids}
        local_edges = [edge for edge in edges if edge["from_question_id"] in local_nodes
                       and edge["to_question_id"] in local_nodes]
        graph = temp / "局部问题图.html"
        graph.write_text(_render_html(module, active_thread, local_nodes, local_edges, generation_id,
                                      snapshot["commit_id"]), encoding="utf-8")
        groups = [{"thread_id": thread_id,
                   "root_title": root_titles[thread_id],
                   "question_ids": [question_id for question_id, question in module_questions.items()
                                    if question["thread_id"] == thread_id]}
                  for thread_id in module["thread_ids"]]
        overview = temp / "模块全景图.html"
        overview.write_text(_render_overview(module, groups, nodes, edges, generation_id,
                                              snapshot["commit_id"]), encoding="utf-8")
        catalog = temp / "问题目录.html"
        module_nodes = {question_id: nodes[question_id] for question_id in module_questions}
        catalog.write_text(_render_catalog(module, module_nodes, generation_id,
                                            snapshot["commit_id"]), encoding="utf-8")
        views = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in (graph, overview, catalog)}
        manifest = {"schema_version": 2, "generation_id": generation_id, "module_id": module_id,
                    "learning_commit_id": snapshot["commit_id"], "root_digest": root_digest,
                    "view_sha256": hashlib.sha256(graph.read_bytes()).hexdigest(),
                    "views": views,
                    "current_question_id": active_thread["current_question_id"] if active_thread else None,
                    "local_node_ids": sorted(local_ids), "root_groups": groups,
                    "nodes": nodes, "edges": edges, "documents": documents}
        Draft202012Validator(SCHEMA).validate(manifest)
        _validate_manifest_semantics(manifest)
        manifest_path = temp / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        _sync_generation(temp)
        destination = generations / generation_id
        if destination.exists():
            shutil.rmtree(temp)
            if _validated_manifest(destination, module_id) is None:
                raise WorkspaceError("existing immutable view generation was modified or corrupted; preserving it for inspection")
        else:
            os.replace(temp, destination)
            _sync_path(generations)
        if os.environ.get("VIDEO_EXTRACT_VIEW_TEST_FAULT") == "before_publish":
            raise OSError("injected failure before view publish")
        current_root.mkdir(exist_ok=True)
        pointer = current_root / f"{module_id}.json"
        atomic_write_json(pointer, {"schema_version": 2, "module_id": module_id,
                                    "generation_id": generation_id, "learning_commit_id": snapshot["commit_id"],
                                    "manifest_sha256": manifest_sha256})
        status_root.mkdir(exist_ok=True)
        atomic_write_json(status_root / f"{module_id}.json", {"sync_state": "current", "learning_commit_id": snapshot["commit_id"]})
        return response(status="completed", workspace=str(config.config_path), result={
            "generation_id": generation_id, "generation_path": str(destination),
            "manifest_path": str(destination / "manifest.json"), "pointer_path": str(pointer),
            "views": {name: str(destination / name) for name in views},
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
    served = _read_pointer(pointer, module_id) if pointer.is_file() else None
    manifest_path = generations / served["generation_id"] / "manifest.json" if served else None
    generation_ok = bool(served and manifest_path and manifest_path.is_file()
                         and hashlib.sha256(manifest_path.read_bytes()).hexdigest() == served.get("manifest_sha256")
                         and _validated_manifest(generations / served["generation_id"], module_id) is not None)
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
    selected = _read_pointer(pointer, module_id)
    if selected is None:
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        result={"question_id": question_id, "content_state": "unsynced"},
                        diagnostics=["served view pointer is invalid"])
    generation = generations / selected["generation_id"]
    manifest_path = generation / "manifest.json"
    manifest = (_validated_manifest(generation, module_id)
                if manifest_path.is_file()
                and hashlib.sha256(manifest_path.read_bytes()).hexdigest() == selected.get("manifest_sha256")
                else None)
    if manifest is None:
        return response(status="recoverable_failure", workspace=str(config.config_path),
                        result={"question_id": question_id, "content_state": "unsynced"},
                        diagnostics=["served view generation is missing, modified, or corrupt"])
    node = manifest["nodes"].get(question_id)
    if node is None:
        return response(status="recoverable_failure", workspace=str(config.config_path), result={"content_state": "unsynced"}, diagnostics=["question is absent from served generation"])
    if node["content_state"] == "pending":
        return response(status="awaiting_model", workspace=str(config.config_path), result={"question_id": question_id, "content_state": "pending"}, next_action={"type": "model", "action": "prepare_explanation"})
    if node["content_state"] != "available" or not node.get("href"):
        return response(status="recoverable_failure", workspace=str(config.config_path), result={"question_id": question_id, "content_state": "broken"}, diagnostics=["explanation locator is invalid; refusing to open document start"])
    document = manifest["documents"][question_id]; path = generation / document["logical_path"]
    return response(status="completed", workspace=str(config.config_path), result={"question_id": question_id,
                    "content_state": "available", "generation_id": selected["generation_id"],
                    "document_path": str(path), "section_id": document["section_id"], "obsidian_href": node["href"]})
