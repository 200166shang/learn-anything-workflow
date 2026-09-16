#!/usr/bin/env python3
"""提取 B 站视频合集的正式字幕，并按合集/分集保存。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from convert_voice_to_article import (
    _find_subtitle_rows,
    _find_subtitle_urls,
    page_title,
    safe_filename,
    write_outputs,
)
from output_paths import platform_output_root


def collection_title(page) -> str:
    for selector in ("h1", ".video-title", ".bpx-player-video-title"):
        try:
            value = page.locator(selector).first.inner_text(timeout=2_000).strip()
            if value:
                return re.sub(r"\s+", " ", value)
        except Exception:
            pass
    return page_title(page, "bilibili_collection").replace("_哔哩哔哩_bilibili", "").strip()


def scan_collection(page, url: str) -> tuple[str, list[dict]]:
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(10_000)
    title = collection_title(page)
    items = page.locator("li.bpx-player-ctrl-eplist-multi-menu-item").evaluate_all(
        """els => els.map((el, index) => ({
            index: index + 1,
            title: (el.textContent || '').trim(),
            cid: el.getAttribute('data-cid') || ''
        })).filter(x => x.title)"""
    )
    if not items:
        raise RuntimeError("没有找到合集分集列表；请确认页面已加载且当前账号可以观看合集")
    return title, items


def extract_one(page, context, item: dict, url: str, wait_seconds: int) -> list[dict]:
    subtitle_urls: set[str] = set()

    def on_response(response) -> None:
        lowered = response.url.lower()
        if not any(token in lowered for token in ("subtitle", "player/wbi", "player.so")):
            return
        try:
            subtitle_urls.update(_find_subtitle_urls(response.json()))
        except Exception:
            pass

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        for _ in range(max(wait_seconds, 1)):
            page.wait_for_timeout(1_000)
            if subtitle_urls:
                page.wait_for_timeout(1_500)
                break

        rows: list[dict] = []
        for subtitle_url in subtitle_urls:
            try:
                response = context.request.get(subtitle_url, headers={"Referer": page.url})
                if response.ok:
                    rows = _find_subtitle_rows(response.json())
                    if rows:
                        break
            except Exception:
                continue
        return sorted({(row["start"], row["end"], row["text"]): row for row in rows}.values(), key=lambda row: row["start"])
    finally:
        page.remove_listener("response", on_response)


def write_manifest(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="合集中的任意一个 B 站视频 URL，例如带 p=4 的地址")
    parser.add_argument("--output-dir", type=Path, default=platform_output_root("bilibili"))
    parser.add_argument("--session-dir", type=Path, default=Path("work/bilibili_browser_session"))
    parser.add_argument("--wait-seconds", type=int, default=12)
    parser.add_argument("--headless", action="store_true", help="使用已登录的独立会话后台运行")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright，请先执行 uv sync") from exc

    parsed = urlparse(args.url)
    match = re.search(r"(BV[0-9A-Za-z]+)", parsed.path)
    if not match:
        raise RuntimeError("URL 中没有识别到 B 站 BV 号")
    bvid = match.group(1)
    # 这个合集的列表在用户提供的 p=4 页面可见；不要强行改成 p=1。
    base_url = args.url
    args.session_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(args.session_dir),
            headless=args.headless,
            args=["--no-sandbox"],
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            title, items = scan_collection(page, base_url)
            collection_dir = args.output_dir / f"{safe_filename(title)}__{bvid}"
            collection_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = collection_dir / "collection.json"
            manifest = {
                "platform": "bilibili",
                "collection_title": title,
                "bvid": bvid,
                "source_url": args.url,
                "created_at": datetime.now().astimezone().isoformat(),
                "total": len(items),
                "items": [],
            }
            write_manifest(manifest_path, manifest)
            print(f"合集：{title}，共 {len(items)} 集")

            for item in items:
                number = int(item["index"])
                item_title = safe_filename(item["title"])
                item_dir = collection_dir / f"{number:03d}_{item_title}"
                item_dir.mkdir(parents=True, exist_ok=True)
                (item_dir / "source").mkdir(parents=True, exist_ok=True)
                item_url = f"https://www.bilibili.com/video/{bvid}?p={number}"
                record = {"index": number, "title": item["title"], "cid": item.get("cid"), "url": item_url, "directory": str(item_dir.relative_to(args.output_dir))}

                if (item_dir / "source" / "transcript.txt").exists() and (item_dir / "source" / "transcript.srt").exists():
                    record["status"] = "done"
                    print(f"[{number:03d}/{len(items):03d}] 已存在，跳过：{item['title']}")
                else:
                    try:
                        print(f"[{number:03d}/{len(items):03d}] 提取：{item['title']}")
                        rows = extract_one(page, context, item, item_url, args.wait_seconds)
                        if rows:
                            write_outputs(rows, item_dir / "source", item["title"], item_url, "bilibili subtitle", "subtitle", "zh")
                            record["status"] = "done"
                            record["segment_count"] = len(rows)
                        else:
                            (item_dir / "source" / "metadata.json").write_text(
                                json.dumps({**record, "status": "no_subtitle", "checked_at": datetime.now().astimezone().isoformat()}, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8",
                            )
                            record["status"] = "no_subtitle"
                    except Exception as exc:
                        record["status"] = "error"
                        record["error"] = str(exc)
                        print(f"  失败：{exc}", file=sys.stderr)

                manifest["items"].append(record)
                write_manifest(manifest_path, manifest)
        finally:
            context.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
