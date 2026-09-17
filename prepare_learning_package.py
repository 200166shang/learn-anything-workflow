#!/usr/bin/env python3
"""从已有视频和字幕生成截图候选、HTML 审阅页和 AI 输入包。"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path


TIME_RE = re.compile(r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{3})")
PTS_RE = re.compile(r"pts_time:(?P<value>\d+(?:\.\d+)?)")
ANCHOR_RE = re.compile(
    r"看这里|看一下|可以看到|如图|这张图|这段代码|代码|流程|参数|配置|终端|命令|函数|公式|注意|坐标|数据结构|解析"
)
DEFAULT_NOTE_TEMPLATE = Path("/Users/syz/.agents/skills/source-notes/assets/note-template.md")


@dataclass
class Cue:
    start: float
    end: float
    text: str


@dataclass
class Candidate:
    id: str
    timestamp: float
    sources: list[str]
    reason: str
    image: str = ""
    evidence_for: str = ""
    ocr: str = ""
    visual_hint: str = "unknown"
    status: str = "candidate"
    note: str = ""


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def load_note_template() -> str:
    path = Path(os.environ.get("VIDEO_LEARNING_NOTE_TEMPLATE", str(DEFAULT_NOTE_TEMPLATE))).expanduser()
    try:
        template = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(
            f"无法读取笔记模板：{path}。请恢复 source-notes skill 的 assets/note-template.md，"
            "或设置 VIDEO_LEARNING_NOTE_TEMPLATE。"
        ) from exc
    if not template:
        raise RuntimeError(f"笔记模板为空：{path}")
    return template


def timestamp_to_seconds(value: str) -> float:
    match = TIME_RE.search(value)
    if not match:
        raise ValueError(f"无法解析字幕时间：{value}")
    return (
        int(match["h"]) * 3600
        + int(match["m"]) * 60
        + int(match["s"])
        + int(match["ms"]) / 1000
    )


def seconds_to_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def parse_srt(path: Path) -> list[Cue]:
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig"))
    cues: list[Cue] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_index = next((index for index, line in enumerate(lines) if " --> " in line), None)
        if time_index is None:
            continue
        try:
            start_raw, end_raw = lines[time_index].split(" --> ", 1)
            text = " ".join(lines[time_index + 1 :]).strip()
            if text:
                cues.append(Cue(timestamp_to_seconds(start_raw), timestamp_to_seconds(end_raw), text))
        except (ValueError, IndexError):
            continue
    return cues


def probe_duration(video: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def scene_times(video: Path, threshold: float) -> list[float]:
    command = [
        "ffmpeg", "-hide_banner", "-i", str(video), "-vf",
        f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    return [float(match["value"]) for match in PTS_RE.finditer(result.stderr)]


def add_time_candidate(candidates: list[dict], timestamp: float, source: str, reason: str) -> None:
    priority = {"transcript-anchor": 3, "scene-change": 2, "fixed-interval": 1}[source]
    for candidate in candidates:
        if abs(candidate["timestamp"] - timestamp) <= 2.0:
            candidate["sources"].add(source)
            candidate["reasons"].add(reason)
            if priority > candidate["priority"]:
                candidate["timestamp"] = timestamp
                candidate["priority"] = priority
            return
    candidates.append({
        "timestamp": timestamp,
        "sources": {source},
        "reasons": {reason},
        "priority": priority,
    })


def select_temporal_candidates(raw: list[dict], duration: float, max_candidates: int) -> list[dict]:
    """在保证全片时间覆盖的前提下优先保留字幕锚点和场景变化。"""
    if len(raw) <= max_candidates or duration <= 0:
        return sorted(raw, key=lambda item: item["timestamp"])
    buckets: list[list[dict]] = [[] for _ in range(max_candidates)]
    for item in raw:
        index = min(max_candidates - 1, int(item["timestamp"] / duration * max_candidates))
        buckets[index].append(item)
    selected: list[dict] = []
    selected_ids: set[int] = set()
    for bucket in buckets:
        if not bucket:
            continue
        winner = max(bucket, key=lambda item: (item["priority"], -item["timestamp"]))
        selected.append(winner)
        selected_ids.add(id(winner))
    if len(selected) < max_candidates:
        remaining = sorted(
            (item for item in raw if id(item) not in selected_ids),
            key=lambda item: (-item["priority"], item["timestamp"]),
        )
        selected.extend(remaining[: max_candidates - len(selected)])
    return sorted(selected[:max_candidates], key=lambda item: item["timestamp"])


def choose_times(video: Path, cues: list[Cue], interval: float, threshold: float, max_candidates: int) -> list[dict]:
    duration = probe_duration(video)
    raw: list[dict] = []
    current = 0.0
    while current < duration:
        add_time_candidate(raw, current, "fixed-interval", f"every {interval:g}s")
        current += interval

    for cue in cues:
        if ANCHOR_RE.search(cue.text):
            timestamp = min(duration - 0.1, (cue.start + cue.end) / 2)
            matched = ANCHOR_RE.findall(cue.text)
            add_time_candidate(raw, timestamp, "transcript-anchor", "keyword: " + ", ".join(dict.fromkeys(matched[:3])))

    for timestamp in scene_times(video, threshold):
        if 0.1 < timestamp < duration:
            add_time_candidate(raw, timestamp, "scene-change", f"scene > {threshold:g}")

    return select_temporal_candidates(raw, duration, max_candidates)


def extract_frame(video: Path, timestamp: float, output: Path) -> None:
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{timestamp:.3f}",
        "-i", str(video), "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", str(output),
    ]
    subprocess.run(command, check=True)


def image_distance(left: Path, right: Path) -> float:
    from PIL import Image, ImageOps

    with Image.open(left) as first, Image.open(right) as second:
        first_pixels = list(ImageOps.grayscale(first).resize((16, 16)).tobytes())
        second_pixels = list(ImageOps.grayscale(second).resize((16, 16)).tobytes())
    return sum(abs(a - b) for a, b in zip(first_pixels, second_pixels)) / (len(first_pixels) * 255)


def classify_ocr(text: str) -> str:
    lowered = text.lower()
    if any(token in lowered for token in ("#include", "void ", "int ", "def ", "ros::", "class ", "struct ")):
        return "code"
    if any(token in lowered for token in ("$ ", "sudo ", "apt ", "rosrun", "roslaunch", "cmake", "bash")):
        return "terminal"
    if any(token in text for token in ("流程", "坐标系", "系统", "模块", "输入", "输出")):
        return "diagram-or-slide"
    return "text" if text.strip() else "unknown"


def optional_ocr(image: Path, enabled: bool) -> str:
    if not enabled or not command_exists("tesseract"):
        return ""
    for language_args in (["-l", "chi_sim+eng"], []):
        result = subprocess.run(
            ["tesseract", str(image), "stdout", *language_args, "--psm", "6"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    return ""

def artifact_dir(output_dir: Path, name: str) -> Path:
    path = output_dir / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def make_candidates(video: Path, cues: list[Cue], output_dir: Path, interval: float, threshold: float, max_candidates: int, ocr: bool) -> list[Candidate]:
    keyframe_dir = artifact_dir(output_dir, "frames") / "keyframes"
    keyframe_dir.mkdir(parents=True, exist_ok=True)
    chosen = choose_times(video, cues, interval, threshold, max_candidates)
    candidates: list[Candidate] = []
    previous_image: Path | None = None
    temporary_pattern = keyframe_dir / "batch_%04d.jpg"
    windows = "+".join(f"between(t\\,{max(0.0, float(raw['timestamp'])):.3f}\\,{max(0.0, float(raw['timestamp'])) + .12:.3f})" for raw in chosen)
    if chosen:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-vf", f"select='{windows}',scale='min(960,iw)':-2", "-fps_mode", "vfr", "-q:v", "5", str(temporary_pattern)], check=True)
    extracted = sorted(keyframe_dir.glob("batch_*.jpg"))
    if len(extracted) < len(chosen):
        for path in extracted: path.unlink(missing_ok=True)
        extracted = []
    for index, raw in enumerate(chosen, 1):
        image_path = keyframe_dir / f"candidate_{index:03d}.jpg"
        if extracted:
            extracted[index - 1].replace(image_path)
        else:
            try: extract_frame(video, raw["timestamp"], image_path)
            except subprocess.CalledProcessError as exc:
                print(f"跳过截图 {raw['timestamp']:.2f}s：ffmpeg 失败（{exc.returncode}）", file=sys.stderr); continue
        if previous_image and image_distance(previous_image, image_path) < 0.025:
            image_path.unlink(missing_ok=True)
            candidates[-1].sources = sorted(set(candidates[-1].sources) | raw["sources"])
            candidates[-1].reason += "; " + "; ".join(sorted(raw["reasons"]))
            continue
        ocr_text = optional_ocr(image_path, ocr)
        candidate = Candidate(
            id=f"frame_{len(candidates) + 1:03d}",
            timestamp=raw["timestamp"],
            sources=sorted(raw["sources"]),
            reason="; ".join(sorted(raw["reasons"])),
            image=str(image_path.relative_to(output_dir)),
            ocr=ocr_text,
            visual_hint=classify_ocr(ocr_text),
        )
        candidates.append(candidate)
        previous_image = image_path
    for index, candidate in enumerate(candidates, 1):
        candidate.id = f"frame_{index:03d}"
    return candidates


def write_contact_sheet(candidates: list[Candidate], output_dir: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    if not candidates:
        return
    tile_width, image_height, caption_height, columns = 320, 180, 42, 4
    rows = math.ceil(len(candidates) / columns)
    sheet = Image.new("RGB", (tile_width * columns, (image_height + caption_height) * rows), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, candidate in enumerate(candidates):
        with Image.open(output_dir / candidate.image) as source:
            image = source.convert("RGB")
            image.thumbnail((tile_width - 12, image_height - 12))
            x = (index % columns) * tile_width + (tile_width - image.width) // 2
            y = (index // columns) * (image_height + caption_height) + (image_height - image.height) // 2
            sheet.paste(image, (x, y))
        base_y = (index // columns) * (image_height + caption_height) + image_height
        label = f"{candidate.id}  {seconds_to_timestamp(candidate.timestamp)[:8]}  {candidate.visual_hint}"
        draw.text(((index % columns) * tile_width + 6, base_y + 5), label, fill="black", font=font)
        draw.text(((index % columns) * tile_width + 6, base_y + 20), ",".join(candidate.sources), fill="gray", font=font)
    sheet.save(artifact_dir(output_dir, "frames") / "contact_sheet.jpg", quality=88, optimize=True)


def write_html(candidates: list[Candidate], output_dir: Path, video: str | Path) -> None:
    review_dir = artifact_dir(output_dir, "review")
    payload = json.dumps([asdict(candidate) for candidate in candidates], ensure_ascii=False)
    cards = []
    for candidate in candidates:
        html_image = os.path.relpath(output_dir / candidate.image, review_dir).replace(os.sep, "/")
        options = "".join(
            f"<option value=\"{value}\"{' selected' if candidate.status == value else ''}>{label}</option>"
            for value, label in (
                ("candidate", "待确认"),
                ("keep", "保留"),
                ("reject", "拒绝"),
                ("retry", "重新抽取"),
                ("annotate", "保留并备注"),
            )
        )
        cards.append(
            f"""<article class=\"card\" data-id=\"{html.escape(candidate.id)}\">
<img src=\"{html.escape(html_image)}\" alt=\"{html.escape(candidate.id)}\">
<div><strong>{html.escape(candidate.id)}</strong> · {html.escape(seconds_to_timestamp(candidate.timestamp))}</div>
<div class=\"muted\">来源：{html.escape(', '.join(candidate.sources))}</div>
<div class=\"muted\">证据主题：{html.escape(candidate.evidence_for or '待判断')}</div>
<div class=\"muted\">OCR：{html.escape(candidate.ocr[:240] or '未检测到 OCR；可人工查看')}</div>
<select class=status>{options}</select>
<input class=note value=\"{html.escape(candidate.note, quote=True)}\" placeholder=\"人工备注（可选）\">
</article>"""
        )
    document = f"""<!doctype html>
<meta charset=\"utf-8\"><title>截图候选审阅</title>
<style>body{{font:15px system-ui;margin:24px;background:#f5f5f5}}button{{padding:8px 14px;margin-right:8px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin-top:18px}}.card{{background:white;padding:10px;border-radius:8px;box-shadow:0 1px 4px #bbb}}img{{width:100%;background:#eee;display:block;margin-bottom:8px}}select,input{{box-sizing:border-box;margin-top:8px;padding:6px;width:100%}}.muted{{color:#666;font-size:12px;margin-top:4px;word-break:break-word}}</style>
<h1>截图候选审阅</h1><p>视频：{html.escape(str(video))}</p>
<button onclick=\"exportReview()\">导出 approved_keyframes.json</button><button onclick=\"markAll('reject')\">全部标记拒绝</button>
<div class=grid>{''.join(cards)}</div>
<script>
const candidates = {payload};
function markAll(status) {{ document.querySelectorAll('.status').forEach(x => x.value = status); }}
function exportReview() {{
  const items = candidates.map((item, index) => {{
    const card = document.querySelectorAll('.card')[index];
    return {{...item, status: card.querySelector('.status').value, note: card.querySelector('.note').value}};
  }});
  const data = {{schema_version: 1, video: {json.dumps(str(video))}, reviewed_at: new Date().toISOString(),
    review_mode: 'human', approved_by: 'user', items,
    approved: items.filter(item => ['keep','annotate'].includes(item.status))}};
  const blob = new Blob([JSON.stringify(data, null, 2)], {{type: 'application/json'}});
  const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = 'approved_keyframes.json'; link.click();
}}
</script>"""
    (review_dir / "contact_sheet.html").write_text(document, encoding="utf-8")


def write_json_outputs(candidates: list[Candidate], output_dir: Path, video: str | Path, review_json: Path | None) -> None:
    review_dir = artifact_dir(output_dir, "review")
    review: dict = {}
    if review_json and review_json.exists():
        review = json.loads(review_json.read_text(encoding="utf-8"))
        review_items = review.get("items", [])
        by_id = {item["id"]: item for item in review_items if item.get("id")}
        used_review_ids: set[str] = set()
        for candidate in candidates:
            item = by_id.get(candidate.id)
            if item and item.get("timestamp") is not None:
                if abs(float(item["timestamp"]) - candidate.timestamp) > 5:
                    item = None
            if item is None:
                timestamped = [
                    entry for entry in review_items
                    if entry.get("id") not in used_review_ids and entry.get("timestamp") is not None
                ]
                if timestamped:
                    closest = min(timestamped, key=lambda entry: abs(float(entry["timestamp"]) - candidate.timestamp))
                    if abs(float(closest["timestamp"]) - candidate.timestamp) <= 5:
                        item = closest
            if item:
                candidate.status = item.get("status", candidate.status)
                candidate.note = item.get("note", "")
                if item.get("id"):
                    used_review_ids.add(item["id"])
    data = {
        "schema_version": 1,
        "video": str(video),
        "created_at": datetime.now().astimezone().isoformat(),
        "candidates": [asdict(candidate) for candidate in candidates],
    }
    (review_dir / "keyframes.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    approved = [asdict(candidate) for candidate in candidates if candidate.status in {"keep", "annotate"}]
    supplied_review = review_json is not None and review_json.exists()
    declared_mode = review.get("review_mode") if supplied_review else None
    if declared_mode in {"model_only", "human"}:
        review_mode = declared_mode
    else:
        review_mode = "model_only" if supplied_review and review_json.name == "codex_prescreen.json" else ("human" if supplied_review else "pending")
    approved_by = review.get("approved_by") if supplied_review else None
    if not approved_by:
        approved_by = "codex" if review_mode == "model_only" else ("user" if review_mode == "human" else None)
    approved_data = {
        "schema_version": 1,
        "video": str(video),
        "created_at": datetime.now().astimezone().isoformat(),
        "review_mode": review_mode,
        "approved_by": approved_by,
        "items": [asdict(candidate) for candidate in candidates],
        "approved": approved,
    }
    (review_dir / "approved_keyframes.json").write_text(json.dumps(approved_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
def write_ai_input(candidates: list[Candidate], output_dir: Path, video: str | Path, transcript: Path) -> None:
    notes_dir = artifact_dir(output_dir, 'notes')
    review_dir = artifact_dir(output_dir, 'review')
    approved = [candidate for candidate in candidates if candidate.status in {'keep', 'annotate'}]
    frame_lines: list[str] = []
    for candidate in approved:
        image = os.path.relpath(output_dir / candidate.image, notes_dir).replace(os.sep, '/')
        evidence = candidate.evidence_for or '待判断'
        note = candidate.note or '无人工备注'
        frame_lines.append(f'- {candidate.id} — {seconds_to_timestamp(candidate.timestamp)} — {image} — 证据主题：{evidence} — {candidate.visual_hint} — {note}')
    if not frame_lines:
        frame_lines = ['（尚未确认截图；先打开 ../review/contact_sheet.html 审阅。）']
    note_template = load_note_template()
    cues = parse_srt(transcript)
    knowledge_lines = []
    chunk_size = max(1, min(12, math.ceil(len(cues) / 8)))
    for index, offset in enumerate(range(0, len(cues), chunk_size), 1):
        block = cues[offset:offset + chunk_size]
        title = re.sub(r"\s+", " ", " ".join(cue.text for cue in block))[:48].strip(" ，。；：") or f"片段 {index}"
        knowledge_lines.append(f"### 知识块 {index}. {title}\n\n- 时间：{seconds_to_timestamp(block[0].start)}–{seconds_to_timestamp(block[-1].end)}\n- 覆盖要求：保留该连续片段中的概念、因果、例子、步骤与必要过渡。")
    review_result = json.loads((review_dir / "approved_keyframes.json").read_text(encoding="utf-8"))
    review_mode = review_result.get("review_mode", "pending")
    approved_by = review_result.get("approved_by") or "未确认"
    content = f'''# Codex 学习资料输入包

## 任务

请根据下面的转写和可用于笔记的画面证据，生成 `notes.md`。只把能够回到时间戳或审阅截图的内容写成确定性结论；听不清、画面不清或证据不足的内容列入“待确认问题”。默认产物仅包含学习笔记；用户明确要求时另行生成自测题。

## 视频

- 文件：`{video}`
- 转写：`{transcript}`

## 画面证据审阅状态

- 模式：`{review_mode}`
- 确认者：`{approved_by}`

## 可用于笔记的画面证据

{chr(10).join(frame_lines)}

## 编号知识块覆盖清单

{chr(10).join(knowledge_lines)}

## 单视频笔记结构（强制，不得改名或省略）

```markdown
{note_template}
```

## 强制图文排版要求

- 学习型视频默认生成完整图文笔记；只有用户明确要求“摘要/速览/简洁版”时才压缩内容和图片。
- 按视频的讲解顺序写成图文交错的学习笔记：先解释知识点，紧接着嵌入对应图片，再说明画面内容、对应讲解和学习价值。
- 每一张“已确认画面证据”都必须在它所支撑的正文附近出现；不要把所有图片集中放在文末，不能生成“文字总结 + 批量图片清单”的格式。
- 不要为了摘要式简洁而合并或删掉关键的过渡讲解；用户确认保留的画面全部使用。
- “一句话结论”只能作为入口，不能替代完整的知识点、例子、电路/代码/操作解释。
- 每张图片都要有结论级字幕时间戳和基于画面的简洁说明；无法确认的内容列入“待确认问题”，不要猜测。
- `核心知识点`必须解释概念、因果、前置条件、例子和过渡；`操作流程 / 代码流程`必须按视频顺序写出可复现步骤和验收方法。
- `学习深度标注`必须包含“必须掌握”和“了解即可”；只根据视频证据划分，不把模型额外补充的课程内容伪装成讲师观点。
- 必须生成`结尾回顾`章节。默认只生成学习笔记；用户明确要求时再生成独立的自测题产物。

## 内容完整性流程：先内容，后图片

- 先阅读 `source/transcript.srt`，按讲解主题切分连续知识块，建立覆盖清单：时间范围、必须保留的结论/例子/步骤、可支撑它的候选画面。
- 再按覆盖清单的顺序写正文；每个包含新概念、因果解释、操作步骤、代码/电路分析、例子或结论的知识块，都必须有对应讲解和字幕时间证据。
- 最后把图片作为对应段落的证据插入。图片不能替代内容覆盖；没有合适图片的知识块也必须保留文字讲解。
- 完成前逐项核对覆盖清单，确认没有被摘要压掉关键段落，也没有只写结论而漏掉推导、例子或过渡解释。

每个重要结论使用结论级证据，例如 `[00:01:20–00:01:42](../source/transcript.srt)`；画面证据必须使用 Markdown 图片嵌入，例如 `![画面说明](../frames/keyframes/candidate_001.jpg)`。不要虚构视频没有出现的细节。

## 原始转写

```text
{transcript.read_text(encoding='utf-8').strip()}
```
'''
    (notes_dir / 'notes_input.md').write_text(content, encoding='utf-8')
    prompt = f'''# Codex 截图预筛任务

请查看 `../frames/contact_sheet.jpg`，结合 `../review/keyframes.json` 和 `../source/transcript.srt`，为每个候选返回一行 JSON：`id`、`timestamp`、`status`（keep/reject/retry/annotate）、`reason`。`timestamp` 必须原样填写 `keyframes.json` 中对应候选的秒数。保存完整结果时记录 `review_mode: model_only` 和 `approved_by: codex`。

只保留能帮助理解代码、终端、PPT、流程图、坐标系、硬件状态或讲解过渡的画面；只有完全重复且没有新增信息的画面才标记 reject，不能因为视觉相似就删除新的步骤、对比、回顾或过渡画面。文字被遮挡或需要邻近时间重抽时标记 retry。默认可在 `contact_sheet.html` 中继续人工确认；用户明确采用模型审阅时，保留 `model_only` 语义。

候选数量：{len(candidates)}
'''
    (review_dir / 'codex_review_prompt.md').write_text(prompt, encoding='utf-8')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', type=Path, help='本地视频路径')
    parser.add_argument('--transcript', type=Path, help='SRT 路径；默认使用视频同目录 transcript.srt')
    parser.add_argument('--output-dir', type=Path, help='学习资料输出目录；默认使用视频所在目录')
    parser.add_argument('--interval', type=float, default=15, help='固定抽帧间隔，默认 15 秒')
    parser.add_argument('--scene-threshold', type=float, default=0.30, help='ffmpeg 场景变化阈值，默认 0.30')
    parser.add_argument('--max-candidates', type=int, default=40)
    parser.add_argument('--review-json', type=Path, help='已从 HTML 导出的确认 JSON；用于重建 approved_keyframes.json 和 notes_input.md')
    parser.add_argument('--no-ocr', action='store_true', help='不尝试调用本机 tesseract')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    video = args.video.expanduser().resolve()
    if not video.exists():
        print(f'找不到视频：{video}', file=sys.stderr)
        return 2
    if not command_exists('ffmpeg') or not command_exists('ffprobe'):
        print('需要 ffmpeg 和 ffprobe', file=sys.stderr)
        return 2
    transcript = (args.transcript or video.with_name('transcript.srt')).expanduser().resolve()
    if not transcript.exists():
        print(f'找不到字幕：{transcript}', file=sys.stderr)
        return 2
    output_dir = (args.output_dir or (video.parent.parent if video.parent.name == 'source' else video.parent)).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cues = parse_srt(transcript)
    candidates = make_candidates(video, cues, output_dir, max(1, args.interval), args.scene_threshold, max(1, args.max_candidates), not args.no_ocr)
    write_json_outputs(candidates, output_dir, video, args.review_json)
    write_contact_sheet(candidates, output_dir)
    write_html(candidates, output_dir, video)
    write_ai_input(candidates, output_dir, video, transcript)
    print(f'生成候选截图：{len(candidates)} 张')
    print(f'审阅页：{output_dir / "review" / "contact_sheet.html"}')
    print(f'联系表：{output_dir / "frames" / "contact_sheet.jpg"}')
    print(f'AI 输入包：{output_dir / "notes" / "notes_input.md"}')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f'失败：{exc}', file=sys.stderr)
        raise SystemExit(1)
