#!/usr/bin/env python3
"""按 B 站正式字幕时间点自动缓存、抽帧，并生成学习资料审阅包。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from bilibili_source import download_cached_video, find_cached_output, source_identity
from convert_voice_to_article import capture_bilibili_subtitles, extract_audio, safe_filename, transcribe, write_outputs
from output_paths import platform_output_root
from prepare_learning_package import (
    Candidate,
    Cue,
    add_time_candidate,
    classify_ocr,
    image_distance,
    optional_ocr,
    parse_srt,
    select_temporal_candidates,
    seconds_to_timestamp,
    write_ai_input,
    write_contact_sheet,
    write_html,
    write_json_outputs,
)


CLAIM_RULES = [
    ("IO 最大输出速度定义", r"最大输出速度|最高输出速度|最大输入速度"),
    ("上升/下降/保持时间", r"上升时间|下降时间|保持时间"),
    ("速度限制因素", r"限制.*速度|限制了|有效电压|波形.*正常|斜坡"),
    ("速度档位与选择", r"低速|中速|高速|档位|如何去选择|满足要求|功耗|EMI"),
    ("通信与应用实例", r"LED|SPI|USB|编码器|波特率|BPS|兆赫兹|赫兹"),
    ("GPIO 概念与片上外设", r"片上外设|GPIO模块|控制.*引脚"),
    ("GPIO 分组与命名", r"GPIOA|GPIOB|GPIOC|GPIOD|PA\d|PB\d|PC\d|PD\d|引脚分组"),
    ("工作模式总览", r"八种工作模式|四种输出模式|输出.*输入.*模式"),
    ("输入输出方向", r"输出和输入|信号流向|从外部.*输入|从芯片内部.*外部"),
    ("LED 输出实验", r"LED|寄存器.*写|点亮|熄灭|闪烁"),
    ("推挽输出", r"推挽|P.?MOS|N.?MOS|交替导通"),
    ("开漏输出与高阻抗", r"开路|开漏|高阻抗|悬空"),
    ("通用与复用", r"通用模式|复用模式|控制权|串口.*PA9|定时器.*引脚"),
    ("GPIO 内部电路", r"内部电路|输出控制电路|复用器|VDD|VSS"),
]


def add_claim_candidate(raw: list[dict], timestamp: float, reason: str, claim: str) -> None:
    add_time_candidate(raw, timestamp, "transcript-anchor", reason)
    nearest = min(raw, key=lambda item: abs(item["timestamp"] - timestamp))
    if abs(nearest["timestamp"] - timestamp) <= 2.0:
        nearest.setdefault("claims", set()).add(claim)


def claim_cue_score(cue: Cue, pattern: str) -> int:
    """优先选择讲解画面、举例或结论附近的字幕，而不是普通重复描述。"""
    score = 0
    if re.search(r"看这里|看一下|可以看到|如图|这张图|比如|举例|注意|所以|因此|也就是说", cue.text):
        score += 3
    if re.search(r"是指|叫做|含义|原理|区别|特点|作用|规则|定义", cue.text):
        score += 2
    if re.search(pattern, cue.text, re.IGNORECASE):
        score += 1
    return score


def choose_subtitle_times(cues: list[Cue], interval: float, max_candidates: int) -> list[dict]:
    if not cues:
        return []
    duration = max(cue.end for cue in cues)
    raw: list[dict] = []
    current = 0.0
    while current < duration:
        add_time_candidate(raw, current, "fixed-interval", f"every {interval:g}s")
        current += interval
    for claim, pattern in CLAIM_RULES:
        matches = [cue for cue in cues if re.search(pattern, cue.text, re.IGNORECASE)]
        if not matches:
            continue
        ranked = sorted(matches, key=lambda cue: (-claim_cue_score(cue, pattern), cue.start))
        selected_cues: list[Cue] = []
        for cue in ranked:
            if all(abs(cue.start - old.start) >= 20 for old in selected_cues):
                selected_cues.append(cue)
            if len(selected_cues) >= 2:
                break
        for cue in sorted(selected_cues, key=lambda item: item.start):
            timestamp = min(duration - 0.1, (cue.start + cue.end) / 2)
            matched = re.findall(pattern, cue.text, re.IGNORECASE)
            reason = "核心主题：" + claim
            if matched:
                reason += "；关键词：" + ", ".join(dict.fromkeys(matched[:2]))
            add_claim_candidate(raw, timestamp, reason, claim)
    selected = select_temporal_candidates(raw, duration, max_candidates)
    for item in selected:
        item["claim"] = "；".join(sorted(item.pop("claims", set())))
    return selected


def seek_video(page, video_locator, timestamp: float) -> None:
    """让页面内视频跳到字幕时间点并暂停，等待 seek 完成。"""
    page.evaluate(
        """async time => {
            const video = document.querySelector('video');
            if (!video) throw new Error('页面中没有 video 元素');
            const target = Math.max(0, time);
            video.pause();
            await new Promise((resolve, reject) => {
                let settled = false;
                const finish = () => {
                    if (settled) return;
                    settled = true;
                    clearTimeout(timer);
                    video.removeEventListener('seeked', finish);
                    resolve();
                };
                const timer = setTimeout(finish, 5000);
                video.addEventListener('seeked', finish, {once: true});
                video.currentTime = target;
            });
            if (video.requestVideoFrameCallback) {
                await new Promise(resolve => video.requestVideoFrameCallback(() => resolve()));
            }
        }""",
        timestamp,
    )
    current = float(video_locator.evaluate("video => video.currentTime"))
    if abs(current - timestamp) > 1.0:
        raise RuntimeError(f"播放器未跳到目标时间：目标 {timestamp:.2f}s，实际 {current:.2f}s")
    page.wait_for_timeout(250)


def capture_bilibili_frames(
    url: str,
    session_dir: Path,
    wait_seconds: int,
    headless: bool,
    candidates: list[dict],
    output_dir: Path,
    ocr: bool,
) -> list[Candidate]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright，请先执行 uv sync") from exc

    keyframe_dir = output_dir / "frames" / "keyframes"
    keyframe_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(session_dir),
            headless=headless,
            args=["--no-sandbox"],
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            print(f"打开 B 站视频页面：{url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            print(f"等待视频加载 {wait_seconds} 秒")
            page.wait_for_timeout(max(1, wait_seconds) * 1000)
            video_locator = page.locator("video").first
            if not video_locator.is_visible(timeout=5_000):
                raise RuntimeError("没有找到可见的视频元素；请确认已登录、视频可播放，并使用 --visible 重试")

            result: list[Candidate] = []
            previous_image: Path | None = None
            for index, raw in enumerate(candidates, 1):
                image_path = keyframe_dir / f"candidate_{index:03d}.jpg"
                try:
                    seek_video(page, video_locator, raw["timestamp"])
                    video_locator.screenshot(path=str(image_path), type="jpeg", quality=90)
                except Exception as exc:
                    print(f"视频元素截图失败 {raw['timestamp']:.2f}s：{exc}；尝试页面截图", file=sys.stderr)
                    try:
                        page.screenshot(path=str(image_path), type="jpeg", quality=90)
                    except Exception as page_exc:
                        print(f"页面截图也失败，跳过：{page_exc}", file=sys.stderr)
                        continue

                if previous_image and image_distance(previous_image, image_path) < 0.025:
                    image_path.unlink(missing_ok=True)
                    result[-1].sources = sorted(set(result[-1].sources) | raw["sources"])
                    result[-1].reason += "; " + "; ".join(sorted(raw["reasons"]))
                    if raw.get("claim"):
                        result[-1].timestamp = raw["timestamp"]
                        claims = set(filter(None, result[-1].evidence_for.split("；")))
                        claims.update(raw["claim"].split("；"))
                        result[-1].evidence_for = "；".join(sorted(claims))
                    continue
                ocr_text = optional_ocr(image_path, ocr)
                result.append(
                    Candidate(
                        id=f"frame_{len(result) + 1:03d}",
                        timestamp=raw["timestamp"],
                        sources=sorted(raw["sources"]),
                        reason="; ".join(sorted(raw["reasons"])),
                        image=str(image_path.relative_to(output_dir)),
                        evidence_for=raw.get("claim", ""),
                        ocr=ocr_text,
                        visual_hint=classify_ocr(ocr_text),
                    )
                )
                previous_image = image_path
            return result
        finally:
            context.close()



def write_manifest(
    output_dir: Path,
    url: str,
    title: str,
    transcript_path: Path,
    source_path: Path,
    candidates: list[Candidate],
    review_json: Path | None,
    quality: int,
) -> None:
    """写入机器可读的处理状态，供后续 Codex 快速判断从哪里继续。"""
    review_dir = output_dir / "review"
    notes_dir = output_dir / "notes"
    frames_dir = output_dir / "frames"

    def relative(path: Path) -> str:
        try:
            return str(path.relative_to(output_dir))
        except ValueError:
            return str(path)

    approved_count = sum(candidate.status in {"keep", "annotate"} for candidate in candidates)
    notes_path = notes_dir / "notes.md"
    review_applied = bool(review_json and review_json.exists())
    status = "notes_complete" if notes_path.exists() else ("evidence_ready" if review_applied else "transcript_ready")
    next_action = {
        "notes_complete": "none",
        "evidence_ready": "codex_generate_notes",
        "transcript_ready": "codex_review_candidates",
    }.get(status, "prepare_source")
    manifest = {
        "schema_version": 2,
        "platform": "bilibili",
        "source_url": url,
        "title": title,
        "identity": source_identity(url),
        "status": status,
        "verified_gate": status,
        "next_action": next_action,
        "updated_at": datetime.now().astimezone().isoformat(),
        "video": relative(source_path),
        "subtitle": relative(transcript_path),
        "quality_limit": f"{quality}p",
        "candidate_count": len(candidates),
        "approved_count": approved_count,
        "artifacts": {
            "video": relative(source_path),
            "transcript_srt": relative(transcript_path),
            "metadata": relative(source_path.parent / "metadata.json"),
            "contact_sheet": relative(frames_dir / "contact_sheet.jpg"),
            "candidate_json": relative(review_dir / "keyframes.json"),
            "approved_json": relative(review_dir / "approved_keyframes.json"),
            "review_html": relative(review_dir / "contact_sheet.html"),
            "codex_review_prompt": relative(review_dir / "codex_review_prompt.md"),
            "notes_input": relative(notes_dir / "notes_input.md"),
            "notes": relative(notes_dir / "notes.md"),
        },
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def extract_frames_batch(video_path: Path, candidates: list[dict], output_dir: Path, ocr: bool) -> list[Candidate]:
    """用一个 ffmpeg 进程按时间点批量抽帧，再做相似度去重。"""
    keyframe_dir = output_dir / "frames" / "keyframes"
    keyframe_dir.mkdir(parents=True, exist_ok=True)
    for old in keyframe_dir.glob("candidate_*.jpg"):
        old.unlink(missing_ok=True)
    for old in keyframe_dir.glob("selected_*.jpg"):
        old.unlink(missing_ok=True)
    if not candidates:
        return []

    # 一次顺序解码，select 出每个目标时间窗附近的帧；这是比逐个 seek 更快的批量方式。
    # 输出文件名保留过滤器时间轴上的帧号，随后按帧率映射回字幕时间点。
    rate_raw = subprocess.check_output(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate", "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        text=True,
    ).strip()
    try:
        numerator, denominator = rate_raw.split("/", 1)
        frame_rate = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        frame_rate = 25.0
    windows = []
    for raw in candidates:
        start = max(0.0, float(raw["timestamp"]))
        windows.append(f"between(t\\,{start:.3f}\\,{start + 0.120:.3f})")
    select_filter = "+".join(windows)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"select='{select_filter}'",
        "-fps_mode",
        "vfr",
        "-frame_pts",
        "1",
        "-q:v",
        "3",
        str(keyframe_dir / "selected_%012d.jpg"),
    ]
    print(f"使用单个 ffmpeg 进程批量抽帧：{len(candidates)} 个时间点")
    subprocess.run(command, check=True)
    extracted = []
    for image_path in sorted(keyframe_dir.glob("selected_*.jpg")):
        try:
            pts = int(image_path.stem.rsplit("_", 1)[1])
        except ValueError:
            continue
        extracted.append((pts / frame_rate, image_path))

    result: list[Candidate] = []
    previous_image: Path | None = None
    used_images: set[Path] = set()
    for raw in candidates:
        target = float(raw["timestamp"])
        available = [(abs(timestamp - target), timestamp, path) for timestamp, path in extracted if path not in used_images]
        if not available:
            raise RuntimeError(f"批量抽帧无法匹配时间点：{target:.3f}s")
        distance, _, image_path = min(available, key=lambda item: item[0])
        # 小鹅通导出的部分视频是可变帧率；ffmpeg 的 frame_pts 与
        # ffprobe 的平均帧率换算可能出现数百毫秒误差，但画面仍然处于
        # 字幕锚点附近。保留一个约 1 秒的容差，避免误报失败。
        if distance > 1.0:
            raise RuntimeError(f"批量抽帧时间误差过大：目标 {target:.3f}s，最近帧误差 {distance:.3f}s")
        used_images.add(image_path)
        final_path = keyframe_dir / f"candidate_{len(result) + 1:03d}.jpg"
        image_path.replace(final_path)
        if previous_image and image_distance(previous_image, final_path) < 0.025:
            final_path.unlink(missing_ok=True)
            result[-1].sources = sorted(set(result[-1].sources) | raw["sources"])
            result[-1].reason += "; " + "; ".join(sorted(raw["reasons"]))
            if raw.get("claim"):
                result[-1].timestamp = raw["timestamp"]
                claims = set(filter(None, result[-1].evidence_for.split("；")))
                claims.update(raw["claim"].split("；"))
                result[-1].evidence_for = "；".join(sorted(claims))
            continue
        ocr_text = optional_ocr(final_path, ocr)
        result.append(
            Candidate(
                id=f"frame_{len(result) + 1:03d}",
                timestamp=raw["timestamp"],
                sources=sorted(raw["sources"]),
                reason="; ".join(sorted(raw["reasons"])),
                image=str(final_path.relative_to(output_dir)),
                evidence_for=raw.get("claim", ""),
                ocr=ocr_text,
                visual_hint=classify_ocr(ocr_text),
            )
        )
        previous_image = final_path
    for _, image_path in extracted:
        image_path.unlink(missing_ok=True)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="B 站视频 URL，例如带 p=4 的合集分集地址")
    parser.add_argument("--output-dir", type=Path, default=platform_output_root("bilibili"))
    parser.add_argument("--session-dir", type=Path, default=Path("work/bilibili_browser_session"))
    parser.add_argument("--transcript", type=Path, help="已有 SRT；提供后跳过字幕捕获")
    parser.add_argument("--title", help="使用已有 SRT 时指定视频标题")
    parser.add_argument("--quality", type=int, default=480, help="下载视频的最高高度，默认 480p")
    parser.add_argument("--transcribe-model", default="small", help="无正式字幕时的本地 Whisper 模型，默认 small")
    parser.add_argument("--wait-seconds", type=int, default=15)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--max-candidates", type=int, default=32)
    parser.add_argument("--review-json", type=Path, help="已有 Codex/人工确认 JSON，用于预填审阅页")
    parser.add_argument("--headless", action="store_true", help="使用已登录会话后台运行；首次运行不要使用")
    parser.add_argument("--no-ocr", action="store_true", help="不尝试调用本机 tesseract")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.session_dir.mkdir(parents=True, exist_ok=True)
    cached_dir = find_cached_output(args.output_dir.expanduser().resolve(), args.url)
    transcript_path: Path | None = None
    source_dir: Path | None = None
    rows: list[dict] = []
    title = args.title or "bilibili_video"

    if args.transcript:
        transcript_path = args.transcript.expanduser().resolve()
        if not transcript_path.exists():
            print(f"找不到字幕：{transcript_path}", file=sys.stderr)
            return 2
        rows = [
            {"start": cue.start, "end": cue.end, "text": cue.text}
            for cue in parse_srt(transcript_path)
        ]
        title = args.title or transcript_path.parent.name
    elif cached_dir and ((cached_dir / "source" / "transcript.srt").exists() or (cached_dir / "transcript.srt").exists()):
        output_dir = cached_dir
        source_dir = output_dir / "source" if (output_dir / "source").is_dir() else output_dir
        title = output_dir.name
        transcript_path = source_dir / "transcript.srt"
        rows = [
            {"start": cue.start, "end": cue.end, "text": cue.text}



            for cue in parse_srt(transcript_path)
        ]
        print(f"复用本地字幕：{transcript_path}")
    else:
        title, rows = capture_bilibili_subtitles(args.url, args.session_dir, args.wait_seconds, args.headless)

    output_dir = locals().get("output_dir", (args.output_dir / safe_filename(title)).expanduser().resolve())
    output_dir.mkdir(parents=True, exist_ok=True)
    source_dir = source_dir or (output_dir / "source")
    source_dir.mkdir(parents=True, exist_ok=True)
    if not rows:
        print("没有找到正式字幕，改用本地 Whisper 转写音频", file=sys.stderr)
        source_path = source_dir / "source.mp4"
        if not source_path.exists():
            source_path = download_cached_video(
                args.url, title, args.session_dir, args.wait_seconds, args.headless, source_dir, max(144, args.quality)
            )
        audio_path = extract_audio(source_path, source_dir)
        rows, detected_language = transcribe(audio_path, source_dir, args.transcribe_model, "zh", "transcribe")
        if not rows:
            print("Whisper 没有识别出可用字幕", file=sys.stderr)
            return 1
        write_outputs(rows, source_dir, title, args.url, args.transcribe_model, "transcribe", detected_language or "zh")
        transcript_path = source_dir / "transcript.srt"
    if transcript_path is None and rows:
        write_outputs(rows, source_dir, title, args.url, "bilibili subtitle", "subtitle", "zh")
        transcript_path = source_dir / "transcript.srt"
    elif transcript_path is None and rows and not (source_dir / "transcript.srt").exists():
        write_outputs(rows, source_dir, title, args.url, "bilibili subtitle", "subtitle", "zh")
        transcript_path = source_dir / "transcript.srt"

    source_path = source_dir / "source.mp4"
    if not source_path.exists():
        source_path = download_cached_video(
            args.url,
            title,
            args.session_dir,
            args.wait_seconds,
            args.headless,
            source_dir,
            max(144, args.quality),
        )

    cues = [Cue(float(row["start"]), float(row["end"]), str(row["text"])) for row in rows]
    time_candidates = choose_subtitle_times(cues, max(1, args.interval), max(1, args.max_candidates))
    candidates = extract_frames_batch(source_path, time_candidates, output_dir, not args.no_ocr)
    review_json = args.review_json.expanduser().resolve() if args.review_json else None
    if review_json is None:
        default_review_json = output_dir / "review" / "codex_prescreen.json"
        if default_review_json.exists():
            review_json = default_review_json
    write_json_outputs(candidates, output_dir, args.url, review_json)
    write_contact_sheet(candidates, output_dir)
    write_html(candidates, output_dir, args.url)
    write_ai_input(candidates, output_dir, args.url, transcript_path)
    write_manifest(output_dir, args.url, title, transcript_path, source_path, candidates, review_json, max(144, args.quality))
    print(f"生成 B 站截图候选：{len(candidates)} 张")
    print(f"审阅页：{output_dir / 'review' / 'contact_sheet.html'}")
    print(f"联系表：{output_dir / 'frames' / 'contact_sheet.jpg'}")
    print(f"AI 输入包：{output_dir / 'notes' / 'notes_input.md'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
