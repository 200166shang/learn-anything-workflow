#!/usr/bin/env python3
"""通过本机 Ollama 调用视觉模型和文字模型。

这个模块只负责本地模型适配，不参与字幕候选生成。模型输入输出都落盘，
这样同一批截图重复运行时可以复用结果，也方便之后替换模型。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


OLLAMA_BASE_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
VISION_MODEL = os.environ.get("VIDEO_VISION_MODEL", "qwen3-vl:2b")
TEXT_MODEL = os.environ.get("VIDEO_TEXT_MODEL", "qwen2.5:7b")
VISION_CACHE_VERSION = 1
NOTES_PROMPT_VERSION = 4


def _request_ollama(model: str, messages: list[dict[str, Any]], timeout: int, json_format: bool = False) -> str:
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0,
            "num_ctx": int(os.environ.get("VIDEO_NUM_CTX", "16384")),
            "num_predict": int(os.environ.get("VIDEO_MAX_OUTPUT_TOKENS", "5000")),
        },
    }
    if json_format:
        body["format"] = "json"
    request = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/chat",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    payload = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code >= 500 and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"Ollama 请求失败（HTTP {exc.code}）：{detail}") from exc
        except urllib.error.URLError as exc:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(
                f"无法连接本地 Ollama（{OLLAMA_BASE_URL}）。请确认 Ollama 正在运行：{exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("Ollama 返回了无法解析的响应") from exc
    if payload is None:
        raise RuntimeError("Ollama 请求未返回结果")
    message = payload.get("message", {})
    content = message.get("content")
    # 部分 Qwen3-VL/Ollama 组合即使关闭 thinking，也会把结构化结果放在 thinking 字段。
    if not isinstance(content, str) or not content.strip():
        content = message.get("thinking")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"Ollama 没有返回文本：{payload}")
    return content.strip()


def _parse_json_response(content: str) -> Any:
    cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = min((index for index in (cleaned.find("{"), cleaned.find("[")) if index >= 0), default=-1)
        if start >= 0:
            end = max(cleaned.rfind("}"), cleaned.rfind("]"))
            if end > start:
                try:
                    return json.loads(cleaned[start : end + 1])
                except json.JSONDecodeError:
                    pass
        raise RuntimeError(f"本地视觉模型没有返回有效 JSON：{content[:500]}")


def _image_fingerprint(image_path: Path, evidence_for: str, reason: str) -> str:
    digest = hashlib.sha256()
    digest.update(image_path.read_bytes())
    digest.update(evidence_for.encode("utf-8"))
    digest.update(reason.encode("utf-8"))
    return digest.hexdigest()


def _read_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "items": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "items": {}}
    if not isinstance(payload, dict) or payload.get("schema_version") != VISION_CACHE_VERSION:
        return {"schema_version": 1, "items": {}}
    items = payload.get("items")
    return payload if isinstance(items, dict) else {"schema_version": 1, "items": {}}


def _save_cache(path: Path, cache: dict[str, Any], model: str) -> None:
    cache["schema_version"] = VISION_CACHE_VERSION
    cache["model"] = model
    cache["updated_at"] = datetime.now().astimezone().isoformat()
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _normalise_vision_item(item: dict[str, Any], candidate: Any) -> dict[str, Any]:
    decision = str(item.get("decision", item.get("status", "uncertain"))).lower().strip()
    if decision in {"accept", "keep", "保留", "yes", "true"}:
        decision = "accept"
        status = "keep"
    elif decision in {"reject", "拒绝", "no", "false"}:
        decision = "reject"
        status = "reject"
    else:
        decision = "uncertain"
        status = "candidate"
    try:
        confidence = float(item.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "candidate_id": getattr(candidate, "id"),
        "decision": decision,
        "status": status,
        "supports_claim": bool(item.get("supports_claim", decision == "accept")),
        "visual_type": str(item.get("visual_type", "unknown")),
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(item.get("reason", "本地视觉模型未提供理由")).strip(),
    }


def review_candidates_locally(candidates: list[Any], output_dir: Path, batch_size: int = 4) -> dict[str, Any]:
    """批量让 Ollama 视觉模型审阅候选，并把结果映射到 Candidate.status。"""
    cache_path = output_dir / "vision_review.json"
    cache = _read_cache(cache_path)
    cache_items: dict[str, Any] = cache.setdefault("items", {})
    pending: list[tuple[Any, Path, str]] = []
    results: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        image_path = output_dir / candidate.image
        if not image_path.exists():
            results[candidate.id] = {
                "candidate_id": candidate.id,
                "decision": "uncertain",
                "status": "candidate",
                "supports_claim": False,
                "visual_type": "missing",
                "confidence": 0.0,
                "reason": "截图文件不存在，无法进行本地视觉审阅",
            }
            continue
        fingerprint = _image_fingerprint(image_path, candidate.evidence_for, candidate.reason)
        cached = cache_items.get(fingerprint)
        if isinstance(cached, dict) and cached.get("candidate_id"):
            result = dict(cached)
            result["candidate_id"] = candidate.id
            results[candidate.id] = result
        else:
            pending.append((candidate, image_path, fingerprint))

    for start in range(0, len(pending), max(1, batch_size)):
        batch = pending[start : start + max(1, batch_size)]
        descriptions = []
        images = []
        for index, (candidate, image_path, _) in enumerate(batch, 1):
            descriptions.append(
                f"图片 {index}：candidate_id={candidate.id}；时间={candidate.timestamp:.3f}s；"
                f"证据主题={candidate.evidence_for or '未标注'}；候选原因={candidate.reason}"
            )
            images.append(base64.b64encode(image_path.read_bytes()).decode("ascii"))
        prompt = """你是视频学习资料的截图筛选器。下面按顺序提供多张候选截图及其字幕证据主题。
请逐张判断图片是否值得进入技术学习笔记。

accept：图片包含能直接帮助学习的 PPT、代码、寄存器、参数表、公式、时序图、电路图、流程图、硬件连接或实验状态，并且基本支撑证据主题。
reject：讲师头像、过渡页、纯字幕、空白页、重复画面或与证据主题无关。
uncertain：可能有价值，但文字太小、画面模糊或无法确认是否支撑主题。

只返回 JSON 对象，不要 Markdown，不要输出图片以外的知识推断：
{"items":[{"candidate_id":"candidate_001","decision":"accept|reject|uncertain","supports_claim":true,"visual_type":"diagram|code|parameters|hardware|slide|speaker|other","confidence":0.0,"reason":"一句中文理由"}]}

候选说明：
""" + "\n".join(descriptions)
        content = _request_ollama(
            VISION_MODEL,
            [{"role": "user", "content": prompt, "images": images}],
            timeout=300,
            json_format=True,
        )
        payload = _parse_json_response(content)
        raw_items = payload.get("items", []) if isinstance(payload, dict) else payload
        by_id = {
            str(item.get("candidate_id")): item
            for item in raw_items
            if isinstance(item, dict) and item.get("candidate_id")
        } if isinstance(raw_items, list) else {}
        for index, (candidate, _, fingerprint) in enumerate(batch):
            raw = by_id.get(candidate.id)
            if raw is None and isinstance(raw_items, list) and index < len(raw_items) and isinstance(raw_items[index], dict):
                raw = raw_items[index]
            result = _normalise_vision_item(raw or {}, candidate)
            result.update({"image_hash": fingerprint, "model": VISION_MODEL})
            results[candidate.id] = result
            cache_items[fingerprint] = result
        _save_cache(cache_path, cache, VISION_MODEL)

    for candidate in candidates:
        result = results.get(candidate.id)
        if not result:
            continue
        candidate.status = result["status"]
        candidate.vision_status = result["decision"]
        candidate.vision_confidence = result["confidence"]
        candidate.vision_type = result["visual_type"]
        candidate.vision_reason = result["reason"]
        candidate.note = f"本地视觉：{result['reason']}"
    _save_cache(cache_path, cache, VISION_MODEL)
    summary = {"model": VISION_MODEL, "total": len(candidates), "pending": len(pending), "items": list(results.values())}
    (output_dir / "vision_review_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def _clean_markdown(content: str) -> str:
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    if content.startswith("```") and content.endswith("```"):
        content = re.sub(r"^```(?:markdown|md)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content).strip()
    return content


NOTE_TOP_LEVEL_HEADINGS = [
    "一句话结论",
    "本视频解决的问题",
    "核心知识点",
    "操作流程 / 代码流程",
    "常见误区",
    "待确认问题",
    "不生成自测题",
    "结尾回顾",
]


def _normalise_note_structure(content: str, title: str) -> str:
    """把模型输出收敛到统一的单视频笔记顶层结构。"""
    replacements = {
        "## 学习目标与操作主线": "## 本视频解决的问题",
        "## 按讲解顺序的证据": "## 操作流程 / 代码流程",
        "## 学习深度标注": "### 学习深度标注",
        "## 关键画面证据": "### 关键画面证据",
        "## 证据范围": "### 证据范围",
        "## 证据索引": "### 证据索引",
    }
    for old, new in replacements.items():
        content = content.replace(old, new)

    def demote_unknown_top_level(match: re.Match[str]) -> str:
        heading = match.group(1).strip()
        return match.group(0) if heading in NOTE_TOP_LEVEL_HEADINGS else f"### {heading}"

    content = re.sub(r"^##\s+(.+?)\s*$", demote_unknown_top_level, content, flags=re.MULTILINE)

    if not re.search(r"^#\s+", content, flags=re.MULTILINE):
        content = f"# {title}\n\n" + content
    if "## 一句话结论" not in content:
        content = f"# {title}\n\n## 一句话结论\n\n{content.lstrip()}"
    if "## 本视频解决的问题" not in content:
        marker = "## 核心知识点"
        content = content.replace(marker, "## 本视频解决的问题\n\n请根据视频内容说明本节要解决的问题。\n\n" + marker, 1)
    if "## 核心知识点" not in content:
        marker = "## 操作流程 / 代码流程"
        content = content.replace(marker, "## 核心知识点\n\n本节核心概念、因果关系和前置条件。\n\n" + marker, 1)
    if "## 操作流程 / 代码流程" not in content:
        marker = "## 常见误区"
        content = content.replace(marker, "## 操作流程 / 代码流程\n\n按视频顺序整理可复现步骤和验收方法。\n\n" + marker, 1)
    if "### 学习深度标注" not in content and "## 操作流程 / 代码流程" in content:
        marker = "## 操作流程 / 代码流程"
        depth = (
            "### 学习深度标注\n\n"
            "#### 必须掌握\n\n- 能解释本视频的核心概念和主流程。\n\n"
            "#### 了解即可\n\n- 固定示例数值、界面位置和可查细节不必死记。\n\n"
        )
        content = content.replace(marker, depth + marker, 1)
    if "## 常见误区" not in content:
        content += "\n\n## 常见误区\n\n- 只记结论而不检查对应的前置条件和证据。"
    if "## 待确认问题" not in content:
        content += "\n\n## 待确认问题\n\n- 字幕或画面证据不足的细节需要回看原视频确认。"
    if "## 不生成自测题" not in content:
        marker = "## 结尾回顾"
        section = "## 不生成自测题\n\n本笔记不附加自测题。\n\n"
        if marker in content:
            content = content.replace(marker, section + marker, 1)
        else:
            content += "\n\n" + section.rstrip()
    if "## 结尾回顾" not in content:
        content += "\n\n## 结尾回顾\n\n回顾本视频目标、核心概念、操作链路和验收结果。"
    return content.strip()


def _note_structure_is_valid(content: str) -> bool:
    headings = re.findall(r"^##\s+(.+?)\s*$", content, flags=re.MULTILINE)
    positions = []
    for heading in NOTE_TOP_LEVEL_HEADINGS:
        try:
            positions.append(headings.index(heading))
        except ValueError:
            return False
    if positions != sorted(positions):
        return False
    legacy = {"学习目标与操作主线", "按讲解顺序的证据", "关键画面证据", "证据范围", "证据索引"}
    return not legacy.intersection(headings) and set(headings).issubset(set(NOTE_TOP_LEVEL_HEADINGS))


def _srt_cues(path: Path) -> list[tuple[float, float, str]]:
    """读取 SRT 的时间和文字，避免把整部超长字幕塞进本地模型上下文。"""
    time_re = re.compile(
        r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{3})"
        r"\s+-->\s+"
        r"(?P<h2>\d{2}):(?P<m2>\d{2}):(?P<s2>\d{2})[,.](?P<ms2>\d{3})"
    )
    cues: list[tuple[float, float, str]] = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig")):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        match = time_re.search(lines[time_index])
        if not match:
            continue
        def to_seconds(prefix: str) -> float:
            group = lambda part: f"{part}{prefix}" if prefix else part
            return (
                int(match[group("h")]) * 3600
                + int(match[group("m")]) * 60
                + int(match[group("s")])
                + int(match[group("ms")]) / 1000
            )
        text = "".join(lines[time_index + 1 :]).strip()
        if text:
            cues.append((to_seconds(""), to_seconds("2"), text))
    return cues


def _timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _select_transcript_evidence(path: Path, candidates: list[Any], max_chars: int = 24000) -> str:
    """按已选截图时间点取字幕邻域，提供给笔记模型可追溯的证据摘录。"""
    cues = _srt_cues(path)
    if not cues:
        return path.read_text(encoding="utf-8").strip()[:max_chars]
    anchors = [candidate for candidate in candidates if candidate.status in {"keep", "annotate"}]
    if not anchors:
        anchors = candidates
    selected: list[tuple[float, float, str]] = []
    for candidate in anchors:
        start = max(0.0, float(candidate.timestamp) - 16)
        end = float(candidate.timestamp) + 20
        selected.extend(cue for cue in cues if cue[1] >= start and cue[0] <= end)
    unique = {(start, end, text): (start, end, text) for start, end, text in selected}
    lines = [f"[{_timestamp(start)}–{_timestamp(end)}] {text}" for start, end, text in sorted(unique.values())]
    if not lines:
        lines = [f"[{_timestamp(start)}–{_timestamp(end)}] {text}" for start, end, text in cues[:80]]
    result: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > max_chars:
            break
        result.append(line)
        size += len(line) + 1
    return "\n".join(result)


def generate_notes_locally(
    candidates: list[Any],
    output_dir: Path,
    title: str,
    source_url: str,
    transcript_path: Path,
) -> Path:
    """根据字幕和本地视觉结果生成最终单视频 Markdown 笔记。"""
    transcript = _select_transcript_evidence(transcript_path, candidates)
    approved = [candidate for candidate in candidates if candidate.status in {"keep", "annotate"}]
    evidence_lines = []
    for candidate in approved:
        evidence_lines.append(
            f"- {candidate.id}；时间 {candidate.timestamp:.3f}s；字幕证据主题：{candidate.evidence_for or '未标注'}；"
            f"图片路径：../{candidate.image}；视觉类型：{getattr(candidate, 'vision_type', 'unknown')}；"
            f"视觉判断：{getattr(candidate, 'vision_reason', '')}"
        )
    if not evidence_lines:
        evidence_lines = ["- 没有自动保留的截图；不要虚构图片证据。"]
    input_material = f"notes-prompt-{NOTES_PROMPT_VERSION}\n{TEXT_MODEL}\n{title}\n{source_url}\n{transcript}\n" + "\n".join(evidence_lines)
    input_hash = hashlib.sha256(input_material.encode("utf-8")).hexdigest()
    metadata_path = output_dir / "local_ai_metadata.json"
    if (output_dir / "notes.md").exists() and metadata_path.exists():
        try:
            old_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if old_metadata.get("input_hash") == input_hash and old_metadata.get("text_model") == TEXT_MODEL:
                return output_dir / "notes.md"
        except (OSError, json.JSONDecodeError):
            pass
    prompt = f"""请把下面一个技术视频整理成可直接学习的 Markdown 笔记。

标题：{title}
来源：{source_url}

要求：
1. 只依据字幕摘录写事实，不要把常识、截图猜测或补充知识伪装成讲师结论。自动转写中明显失真的专有名词可按标题和上下文更正（例如“约了威溜”应写作 YOLOv6），其余听不清处放入“待确认问题”。
2. 写成能独立阅读的技术讲解，而不是把字幕换序或罗列结论：每个核心小节要说明概念是什么、为何需要它、它与前后步骤如何衔接，并保留视频给出的例子、比较、配置或验收方式。
3. 核心知识点、流程步骤、常见误区中每一个可核验结论末尾都附结论级字幕链接，格式严格为 `[00:01:20.000–00:01:42.000](../transcript.srt)`。不允许使用裸时间戳或错误的 `transcript.srt` 相对路径。
4. 只有在下方“已确认画面证据”中出现的图片才可使用。每张批准图片必须各出现一次，嵌在其时间附近的核心知识点或流程步骤中；图片后紧接一句只描述可确认学习价值的画面说明。绝不另设“画面证据/图片清单”章节，绝不以泛化的“用于核对”敷衍。
5. 学习型视频必须形成完整、按视频顺序推进的图文笔记。不要压缩成短列表，也不要让截图替代解释；没有截图的关键过渡仍必须用正文和时间证据解释。
6. `学习深度标注` 是 `核心知识点` 下的三级标题，必须分别给出具体的“必须掌握”和“了解即可”，且只根据本视频内容划分。
7. `操作流程 / 代码流程` 必须按视频顺序编号；写明输入、处理、界面或代码变化、输出/验收。视频若以原理讲解为主，应明确这是一条理解/选型链路而非虚构可执行命令。
8. `待确认问题` 只记录证据不足之处；没有问题时明确写“本次摘录中没有必须回看才能使用的关键结论”。`不生成自测题` 只写明本笔记不附题目。
9. 输出完整 Markdown，不要输出解释、代码围栏或“以下是笔记”等前言。

固定结构（顶层标题必须按此顺序，不能改名、删除或增加旧模板标题）：
# {title}
## 一句话结论
## 本视频解决的问题
## 核心知识点
### [按概念或原理编号的小节]
### 学习深度标注
#### 必须掌握
#### 了解即可
## 操作流程 / 代码流程
### [按视频顺序编号的步骤]
## 常见误区
## 待确认问题
## 不生成自测题
## 结尾回顾

禁止使用这些旧的顶层标题：`学习目标与操作主线`、`按讲解顺序的证据`、`关键画面证据`、`证据范围`、`证据索引`。图片应嵌入核心知识点或操作步骤附近，不要集中放到单独的图片章节。

已确认画面证据：
{chr(10).join(evidence_lines)}

    与截图时间点对应的字幕证据摘录（不是完整字幕；不要补写摘录之外的细节）：
---
{transcript}
---
"""
    try:
        request_timeout = int(os.environ.get("VIDEO_REQUEST_TIMEOUT", "300"))
        content = _clean_markdown(
            _request_ollama(TEXT_MODEL, [{"role": "user", "content": prompt}], timeout=request_timeout)
        )
    except RuntimeError as exc:
        # 本地文字模型失败时仍产出可追溯的基础笔记，方便用户继续使用流程。
        fallback_evidence = []
        for index, candidate in enumerate(approved, 1):
            fallback_evidence.append(
                f"### {index}. 依据字幕和画面整理的关键内容\n\n"
                f"本段需要结合视频字幕进一步理解，当前可确认的证据时间为 "
                f"[{_timestamp(candidate.timestamp)}](transcript.srt)。\n\n"
                f"![画面证据 {candidate.id}]({candidate.image})\n\n"
                "画面说明：该截图是本段字幕讲解的已批准画面证据。"
            )
        if not fallback_evidence:
            fallback_evidence = ["### 1. 原始字幕内容\n\n" + transcript]
        content = (
            f"# {title}\n\n"
            f"> 本地文字模型生成失败：{exc}\n\n"
            "## 一句话结论\n\n请依据下方字幕和画面复述本视频的核心结论。\n\n"
            "## 本视频解决的问题\n\n本视频的目标需要结合原始字幕确认。\n\n"
            "## 核心知识点\n\n"
            + "\n\n".join(fallback_evidence[:max(1, min(2, len(fallback_evidence)))])
            + "\n\n### 学习深度标注\n\n#### 必须掌握\n\n- 掌握视频明确讲解的核心概念和主流程。\n\n#### 了解即可\n\n- 固定示例、界面位置和未展开的实现细节。\n\n"
            "## 操作流程 / 代码流程\n\n"
            + "\n\n".join(fallback_evidence[2:])
            + "\n\n## 常见误区\n\n- 不要把字幕或画面中无法确认的细节当成事实。\n\n"
            "## 待确认问题\n\n- 本地模型生成失败，需要回看原视频补齐讲解。\n\n"
            "## 不生成自测题\n\n本笔记不附加自测题。\n\n"
            "## 结尾回顾\n\n本笔记保留了可追溯的字幕和画面证据，完整讲解需在模型恢复后重新生成。\n"
        )
    content = _normalise_note_structure(content, title)
    if not _note_structure_is_valid(content):
        raise RuntimeError("本地文字模型输出未通过强制单视频笔记模板校验")
    notes_path = output_dir / "notes.md"
    notes_path.write_text(content.rstrip() + "\n", encoding="utf-8")
    metadata = {
        "schema_version": 1,
        "text_model": TEXT_MODEL,
        "vision_model": VISION_MODEL,
        "approved_count": len(approved),
        "input_hash": input_hash,
        "created_at": datetime.now().astimezone().isoformat(),
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return notes_path
