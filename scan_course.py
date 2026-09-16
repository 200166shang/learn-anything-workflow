#!/usr/bin/env python3
"""扫描已登录的小鹅通课程目录，保存可供后续批量下载使用的清单。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from output_paths import platform_output_root
from video_extract.contracts import SCHEMA_VERSION
from video_extract.manifest import atomic_write_json


CATALOG_ENDPOINT = "e_course.resource_catalog_list.get"


def flatten_catalog(items: list[dict], course_url: str) -> tuple[list[dict], list[dict]]:
    sections: list[dict] = []
    lessons: list[dict] = []
    section_by_id: dict[str, str] = {}

    def visit(item: dict, section_title: str = "") -> None:
        chapter_type = item.get("chapter_type")
        current_section = section_title
        if chapter_type == 1:
            current_section = item.get("chapter_title") or item.get("resource_title") or "未命名章节"
            section_id = item.get("chapter_id") or item.get("resource_id")
            if section_id:
                section_by_id[str(section_id)] = current_section
            sections.append({
                "title": current_section,
                "chapter_id": section_id,
                "sort_value": item.get("sort_value"),
                "lesson_count": item.get("section_num", 0),
            })
        elif chapter_type == 2:
            relative_url = item.get("jump_url") or item.get("url")
            if not relative_url:
                resource_id = item.get("resource_id") or item.get("chapter_id")
                relative_url = f"/p/course/video/{resource_id}" if resource_id else ""
            full_url = urljoin(course_url, relative_url)
            parts = urlsplit(full_url)
            query = dict(parse_qsl(parts.query))
            if item.get("course_id"):
                query.setdefault("product_id", item["course_id"])
            query.setdefault("auto", "true")
            full_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
            lessons.append({
                "index": len(lessons) + 1,
                "title": item.get("chapter_title") or item.get("resource_title") or "未命名视频",
                "video_id": item.get("resource_id") or item.get("chapter_id"),
                "url": full_url,
                "resource_type": item.get("resource_type"),
                "is_video": item.get("resource_type") == 3,
                "section": current_section or section_by_id.get(str(item.get("p_id")), "未分类"),
                "sort_value": item.get("sort_value"),
                "duration_seconds": item.get("video_length", 0),
                "unlock_state": item.get("unlock_state"),
                "study_status": item.get("study_status"),
                "learn_progress": item.get("learn_progress", 0),
            })
        for child in item.get("children") or []:
            visit(child, current_section)

    for item in items:
        visit(item)
    sections.sort(key=lambda item: (item["sort_value"] is None, item["sort_value"] or 0))
    lessons.sort(key=lambda item: (item["sort_value"] is None, item["sort_value"] or 0))
    for index, lesson in enumerate(lessons, 1):
        lesson["index"] = index
    return sections, lessons


def scan_course(course_url: str, session_dir: Path, wait_seconds: int, headless: bool) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright。请先执行：uv sync && uv run playwright install chromium") from exc

    catalog_lists: list[list[dict]] = []
    with sync_playwright() as playwright:
        session_dir.mkdir(parents=True, exist_ok=True)
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(session_dir),
            headless=headless,
            args=["--no-sandbox"],
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else context.new_page()

        def on_response(response) -> None:
            if CATALOG_ENDPOINT not in response.url:
                return
            try:
                payload = response.json()
                if payload.get("code") == 0 and isinstance(payload.get("data", {}).get("list"), list):
                    catalog_lists.append(payload["data"]["list"])
            except Exception:
                pass

        page.on("response", on_response)
        try:
            print(f"扫描课程：{course_url}")
            page.goto(course_url, wait_until="domcontentloaded", timeout=60_000)
            print(f"等待目录加载 {wait_seconds} 秒；未登录时请改用 --visible 并在浏览器中登录。")
            page.wait_for_timeout(max(1, wait_seconds) * 1000)

            # 该课程页面先返回章节标题，再按展开动作返回对应章节的小节。
            # 逐个点击章节标题，让懒加载目录全部出现；点击失败时保留已捕获结果。
            section_titles = []
            for item in catalog_lists[0] if catalog_lists else []:
                if item.get("chapter_type") == 1:
                    title = item.get("chapter_title") or item.get("resource_title")
                    if title:
                        section_titles.append(title)
            for section_title in section_titles:
                try:
                    locator = page.get_by_text(section_title, exact=False).first
                    locator.click(timeout=2_000)
                    page.wait_for_timeout(700)
                except Exception:
                    continue
        finally:
            title = page.title().strip() or "未命名课程"
            context.close()

    if not catalog_lists:
        raise RuntimeError("没有捕获到课程目录接口。请确认课程地址正确、账号有权限，并尝试 --visible --wait-seconds 30")

    # 合并“章节标题”和“展开章节小节”的多次响应，并按资源 ID 去重。
    items_by_id: dict[str, dict] = {}
    for items in catalog_lists:
        for item in items:
            item_id = str(item.get("resource_id") or item.get("chapter_id") or item.get("id"))
            items_by_id[item_id] = item
    sections, lessons = flatten_catalog(list(items_by_id.values()), course_url)
    return {
        "schema_version": SCHEMA_VERSION,
        "scanned_at": datetime.now().astimezone().isoformat(),
        "course_url": course_url,
        "course_title": title,
        "catalog_available": True,
        "subscribed": True,
        "section_count": len(sections),
        "lesson_count": len(lessons),
        "video_count": sum(item["is_video"] for item in lessons),
        "sections": sections,
        "lessons": lessons,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("course_url", help="课程主页 URL，例如 /p/course/ecourse/course_xxx")
    parser.add_argument("--output", type=Path, default=platform_output_root("xiaoe") / "小沫ROS智能体机器人课程" / "course_catalog.json")
    parser.add_argument("--browser-session", type=Path, default=Path("work/browser_session"))
    parser.add_argument("--wait-seconds", type=int, default=12)
    parser.add_argument("--visible", action="store_true", help="显示浏览器，首次登录时使用")
    args = parser.parse_args()

    try:
        catalog = scan_course(args.course_url, args.browser_session, args.wait_seconds, not args.visible)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(args.output, catalog)
        print(f"课程：{catalog['course_title']}")
        print(f"章节：{catalog['section_count']}，资源：{catalog['lesson_count']}，视频：{catalog['video_count']}")
        print(f"清单已保存：{args.output.resolve()}")
        for lesson in catalog["lessons"][:5]:
            print(f"  {lesson['index']:>3}. [{lesson['section']}] {lesson['title']}")
        if catalog["lesson_count"] > 5:
            print("  ...")
        return 0
    except Exception as exc:
        print(f"扫描失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
