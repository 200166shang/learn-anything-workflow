"""Resumable Xiaoe course acquisition with one persistent background browser."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from .contracts import SCHEMA_VERSION
from .manifest import atomic_write_json
from .workspace import WorkspaceConfig


CATALOG_ENDPOINT = "e_course.resource_catalog_list.get"
LOGIN_MARKERS = ("/login/", "/noPermission/", "/no_permission/")

CATALOG_SCRIPT = r"""async () => {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  for (const node of document.querySelectorAll('span,div,p')) {
    const text = (node.textContent || '').trim();
    if (node.children.length === 0 && ['目录','展开更多','加载更多','点击加载更多'].includes(text)) {
      try { node.click(); } catch (_) {}
    }
  }
  for (let i = 0; i < 20; i++) {
    window.scrollTo(0, document.body.scrollHeight);
    for (const node of document.querySelectorAll('.scroll-view,.list-wrap,.scroller,#app')) {
      if (node.scrollHeight > node.clientHeight) node.scrollTop = node.scrollHeight;
    }
    await sleep(250);
  }
  const app = document.querySelector('#app');
  const root = app && app.__vue__;
  const found = [];
  const seen = new Set();
  function add(value, section='') {
    if (!value || typeof value !== 'object') return;
    const id = value.resource_id || value.chapter_id || value.id;
    const title = value.chapter_title || value.resource_title || value.title || value.name;
    if (!id || !title || seen.has(String(id))) return;
    const type = Number(value.chapter_type || value.resource_type || 0);
    if (!/^[pvlai]_/.test(String(id)) && ![1,2,3,4].includes(type)) return;
    seen.add(String(id));
    found.push({
      chapter_id: value.chapter_id, resource_id: value.resource_id || value.chapter_id,
      chapter_title: title, resource_title: title, chapter_type: type === 1 ? 1 : 2,
      resource_type: type === 4 ? 3 : (type || 3), sort_value: value.sort_value,
      jump_url: value.jump_url || value.h5_url || value.url || '', section_title: section,
      course_id: value.course_id || value.product_id || ''
    });
  }
  function walk(value, depth=0, section='') {
    if (!value || depth > 9) return;
    if (Array.isArray(value)) { for (const child of value) walk(child, depth + 1, section); return; }
    if (typeof value !== 'object') return;
    const title = value.chapter_title || value.resource_title || value.title || section;
    add(value, section);
    for (const [key, child] of Object.entries(value)) {
      if (key.startsWith('_') || key.startsWith('$')) continue;
      if (typeof child === 'object') walk(child, depth + 1, title || section);
    }
  }
  function walkVm(vm, depth=0) {
    if (!vm || depth > 8) return;
    try { walk(vm.$data, 0, ''); } catch (_) {}
    for (const child of (vm.$children || [])) walkVm(child, depth + 1);
  }
  walkVm(root);
  if (!found.length) {
    for (const a of document.querySelectorAll('a[href]')) {
      const href = a.href || '';
      const id = (href.match(/[pvlai]_[A-Za-z0-9]+/) || [])[0];
      const title = (a.textContent || '').trim();
      if (id && title && !seen.has(id)) {
        seen.add(id); found.push({resource_id:id, chapter_title:title, chapter_type:2,
          resource_type: href.includes('/audio/') ? 3 : 3, jump_url:href});
      }
    }
  }
  return found;
}"""

PLAY_SCRIPT = r"""async () => {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  function findM3u8(value, depth=0, seen=new Set()) {
    if (depth > 9 || value == null) return '';
    if (typeof value === 'string') return value.includes('.m3u8') ? value.replaceAll('\\u0026','&').replaceAll('\\/','/') : '';
    if (typeof value !== 'object' || seen.has(value)) return '';
    seen.add(value);
    for (const key of Object.keys(value)) {
      if (key.startsWith('_') || key.startsWith('$')) continue;
      let hit = ''; try { hit = findM3u8(value[key], depth + 1, seen); } catch (_) {}
      if (hit) return hit;
    }
    return '';
  }
  const app = document.querySelector('#app');
  const userId = (document.cookie.match(/ctx_user_id=([^;]+)/)||[])[1]
    || window.__user_id || window.pushData?.payload?.userId || '';
  for (let attempt = 0; attempt < 30; attempt++) {
    for (const entry of performance.getEntriesByType('resource')) if (entry.name.includes('.m3u8')) return {url:entry.name,userId,method:'performance'};
    let vm = app && app.__vue__, hit = '';
    function walkVm(node, depth=0) { if (!node || depth > 8 || hit) return; hit = findM3u8(node.$data); for (const c of (node.$children||[])) walkVm(c, depth+1); }
    walkVm(vm); if (hit) return {url:hit,userId,method:'vue'};
    const media = document.querySelector('video,audio');
    if (media) { try { media.muted = true; await media.play(); } catch (_) { try { media.click(); } catch (_) {} } }
    await sleep(500);
  }
  return {url:'',userId,method:''};
}"""


def inherit_signed_query(segment_url: str, playlist_url: str) -> str:
    """Resolve an HLS child URL while preserving the playlist's raw signature."""
    resolved = urljoin(playlist_url, segment_url)
    playlist_query = urlparse(playlist_url).query
    if not playlist_query:
        return resolved
    parsed = urlparse(resolved)
    existing = {part.split("=", 1)[0] for part in parsed.query.split("&") if part}
    missing = [part for part in playlist_query.split("&") if part.split("=", 1)[0] not in existing]
    if not missing:
        return resolved
    separator = "&" if parsed.query else "?"
    return resolved + separator + "&".join(missing)


def xor_key(key: bytes, user_id: str) -> bytes:
    identity = user_id.encode("utf-8")
    if not identity:
        return key
    return bytes(value ^ identity[index % len(identity)] for index, value in enumerate(key))


def _http_bytes(url: str, headers: dict[str, str]) -> bytes:
    request = Request(url, headers={key.title(): value for key, value in headers.items()})
    with urlopen(request, timeout=60) as response:
        return response.read()


def _ts_valid(data: bytes) -> bool:
    return len(data) >= 188 * 3 and any(
        all(offset + step * 188 < len(data) and data[offset + step * 188] == 0x47 for step in range(3))
        for offset in range(min(188, len(data)))
    )


def _openssl_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    result = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex(), "-nopad"],
        input=data, capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def download_xiaoe_hls(candidate, output_dir: Path, user_id: str) -> Path:
    """Download signed Xiaoe HLS, including its optional user-bound AES key."""
    headers = dict(candidate.headers)
    headers.setdefault("referer", candidate.referer)
    headers.setdefault("user-agent", "Mozilla/5.0")
    playlist_url = candidate.url
    playlist = _http_bytes(playlist_url, headers).decode("utf-8", "replace")
    variants = [line.strip() for line in playlist.splitlines() if line.strip() and not line.startswith("#")]
    if "#EXT-X-STREAM-INF" in playlist:
        if not variants:
            raise RuntimeError("主播放清单没有可用码率")
        playlist_url = inherit_signed_query(variants[-1], playlist_url)
        playlist = _http_bytes(playlist_url, headers).decode("utf-8", "replace")
    segments = [line.strip() for line in playlist.splitlines() if line.strip() and not line.startswith("#")]
    if not segments:
        raise RuntimeError("m3u8 没有媒体分片")
    key_match = re.search(r'#EXT-X-KEY:[^\n]*URI="([^"]+)"', playlist)
    iv_match = re.search(r"#EXT-X-KEY:[^\n]*IV=0x([0-9a-fA-F]+)", playlist)
    media_sequence = int((re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", playlist) or [None, "0"])[1])
    raw_key = None
    if key_match:
        key_url = inherit_signed_query(key_match.group(1), playlist_url)
        try:
            raw_key = _http_bytes(key_url, headers)
        except Exception:
            separator = "&" if "?" in key_url else "?"
            raw_key = _http_bytes(f"{key_url}{separator}uid={user_id}", headers)
        if len(raw_key) != 16:
            separator = "&" if "?" in key_url else "?"
            raw_key = _http_bytes(f"{key_url}{separator}uid={user_id}", headers)
        if len(raw_key) != 16:
            raise RuntimeError("小鹅通 AES Key 长度不是 16 字节")
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="xiaoe-hls-", dir=output_dir) as raw_tmp:
        temp = Path(raw_tmp)
        concat = temp / "files.txt"
        chosen_key: bytes | None = None
        with concat.open("w", encoding="utf-8") as listing:
            for index, segment in enumerate(segments):
                data = _http_bytes(inherit_signed_query(segment, playlist_url), headers)
                if raw_key:
                    iv = bytes.fromhex(iv_match.group(1).zfill(32)) if iv_match else (media_sequence + index).to_bytes(16, "big")
                    keys = [chosen_key] if chosen_key else [xor_key(raw_key, user_id), raw_key]
                    decoded = next((value for key in keys if key for value in [_openssl_decrypt(data, key, iv)] if _ts_valid(value)), None)
                    if decoded is None:
                        raise RuntimeError(f"第 {index + 1} 个分片无法使用 raw/XOR Key 解密")
                    if chosen_key is None:
                        chosen_key = next(key for key in keys if _ts_valid(_openssl_decrypt(data, key, iv)))
                    data = decoded
                segment_path = temp / f"{index:06d}.ts"
                segment_path.write_bytes(data)
                listing.write(f"file '{segment_path.name}'\n")
        target = output_dir / "source.mp4"
        merged = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(target)],
            cwd=temp, capture_output=True, text=True,
        )
        if merged.returncode != 0:
            raise RuntimeError(f"ffmpeg 合并失败：{merged.stderr.strip()}")
        return target


def course_id(course_url: str) -> str:
    match = re.search(r"(course_[A-Za-z0-9]+)", course_url)
    if not match:
        raise ValueError("小鹅通课程链接中缺少 course_... 标识")
    return match.group(1)


def parse_chapters(value: str) -> set[int]:
    chapters: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = (int(item.strip()) for item in part.split("-", 1))
            if start < 1 or end < start:
                raise ValueError(f"无效章节范围：{part}")
            chapters.update(range(start, end + 1))
        else:
            number = int(part)
            if number < 1:
                raise ValueError(f"无效章节号：{part}")
            chapters.add(number)
    if not chapters:
        raise ValueError("至少指定一个章节，例如 --chapters 17,18,19")
    return chapters


def _chinese_number(value: str) -> int | None:
    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        return (digits.get(left, 1) * 10) + digits.get(right, 0)
    return digits.get(value)


def lesson_chapter(item: dict[str, Any]) -> int | None:
    title = str(item.get("title") or "")
    match = re.match(r"\s*(\d+)\.", title)
    if match:
        return int(match.group(1))
    section = str(item.get("section") or "")
    match = re.search(r"第([零一二三四五六七八九十]+)章", section)
    return _chinese_number(match.group(1)) if match else None


def select_lessons(catalog: dict[str, Any], chapters: set[int]) -> list[dict[str, Any]]:
    return [
        item for item in catalog.get("lessons", [])
        if item.get("is_video") and lesson_chapter(item) in chapters
    ]


def paths(config: WorkspaceConfig, course_url: str) -> dict[str, Path]:
    identity = course_id(course_url)
    host = re.sub(r"[^A-Za-z0-9._-]+", "_", urlparse(course_url).hostname or "xiaoe")
    catalog_root = config.media / "catalog" / "xiaoe"
    return {
        "session": config.media / "sessions" / "xiaoe" / host,
        "catalog": catalog_root / f"{identity}.json",
        "state": catalog_root / f"{identity}.download.json",
        "items": config.media / "items" / "xiaoe",
    }


def _authorized(url: str) -> bool:
    return not any(marker.lower() in url.lower() for marker in LOGIN_MARKERS)


def _scan_page(page, course_url: str, wait_seconds: int) -> dict[str, Any]:
    from scan_course import flatten_catalog

    catalog_lists: list[list[dict[str, Any]]] = []

    def on_response(response) -> None:
        if CATALOG_ENDPOINT not in response.url:
            return
        try:
            payload = response.json()
            rows = payload.get("data", {}).get("list")
            if payload.get("code") == 0 and isinstance(rows, list):
                catalog_lists.append(rows)
        except Exception:
            pass

    page.on("response", on_response)
    try:
        page.goto(course_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(max(1, wait_seconds) * 1000)
        if not _authorized(page.url):
            raise PermissionError("小鹅通登录已失效，请先运行 video-extract xiaoe login")
        section_titles = []
        for item in catalog_lists[0] if catalog_lists else []:
            if item.get("chapter_type") == 1:
                title = item.get("chapter_title") or item.get("resource_title")
                if title:
                    section_titles.append(title)
        for title in section_titles:
            try:
                page.get_by_text(title, exact=False).first.click(timeout=2_000)
                page.wait_for_timeout(500)
            except Exception:
                continue
    finally:
        page.remove_listener("response", on_response)

    if not catalog_lists:
        rendered = page.evaluate(CATALOG_SCRIPT)
        if rendered:
            catalog_lists.append(rendered)
    if not catalog_lists:
        raise RuntimeError("没有捕获到课程目录；账号可能未登录、无课程权限，或页面尚未渲染完成")
    items_by_id: dict[str, dict[str, Any]] = {}
    for rows in catalog_lists:
        for item in rows:
            identity = str(item.get("resource_id") or item.get("chapter_id") or item.get("id"))
            items_by_id[identity] = item
    sections, lessons = flatten_catalog(list(items_by_id.values()), course_url)
    return {
        "schema_version": SCHEMA_VERSION,
        "scanned_at": datetime.now().astimezone().isoformat(),
        "course_url": course_url,
        "course_id": course_id(course_url),
        "course_title": page.title().strip() or course_id(course_url),
        "catalog_available": True,
        "subscribed": True,
        "section_count": len(sections),
        "lesson_count": len(lessons),
        "video_count": sum(bool(item.get("is_video")) for item in lessons),
        "sections": sections,
        "lessons": lessons,
    }


def _capture_page(context, item: dict[str, Any], wait_seconds: int):
    from convert_voice_to_article import MediaCandidate, media_score

    page = context.new_page()
    candidates: dict[str, MediaCandidate] = {}

    def on_request(request) -> None:
        if ".m3u8" not in request.url.lower():
            return
        candidate = MediaCandidate(
            url=request.url,
            referer=request.headers.get("referer", item["url"]),
            page_url=page.url,
            title=item["title"],
            score=media_score(request.url),
            headers={str(key): str(value) for key, value in request.all_headers().items()},
        )
        previous = candidates.get(request.url)
        if previous is None or candidate.score > previous.score:
            candidates[request.url] = candidate

    page.on("request", on_request)
    try:
        page.goto(item["url"], wait_until="domcontentloaded", timeout=60_000)
        if not _authorized(page.url):
            raise PermissionError("小鹅通登录已失效，请先运行 video-extract xiaoe login")
        for _ in range(max(1, wait_seconds)):
            if candidates:
                page.wait_for_timeout(800)
                break
            try:
                page.locator("video").first.evaluate("el => { el.muted = true; return el.play(); }")
            except Exception:
                try:
                    page.locator("video").first.click(position={"x": 20, "y": 20}, timeout=800, force=True)
                except Exception:
                    pass
            page.wait_for_timeout(1000)
        if not candidates:
            captured = page.evaluate(PLAY_SCRIPT)
            if captured and captured.get("url"):
                candidate = MediaCandidate(
                    url=captured["url"], referer=item["url"], page_url=page.url,
                    title=item["title"], score=media_score(captured["url"]),
                    headers={"referer": item["url"]},
                )
                candidate.user_id = captured.get("userId", "")
                candidates[candidate.url] = candidate
    finally:
        page.close()
    return max(candidates.values(), key=lambda item: item.score) if candidates else None


def _video_ok(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    checked = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return checked.returncode == 0 and bool(checked.stdout.strip())


def _load_state(path: Path, course_url: str, lessons: list[dict[str, Any]]) -> dict[str, Any]:
    if path.is_file():
        state = json.loads(path.read_text(encoding="utf-8"))
    else:
        state = {"schema_version": 1, "course_url": course_url, "items": {}}
    for item in lessons:
        state["items"].setdefault(str(item["video_id"]), {"title": item["title"], "status": "pending"})
    state["updated_at"] = datetime.now().astimezone().isoformat()
    return state


def login(config: WorkspaceConfig, course_url: str, wait_seconds: int = 300) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    resolved = paths(config, course_url)
    resolved["session"].mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(resolved["session"]), headless=False, args=["--no-sandbox"], viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(course_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            # Xiaoe often aborts the initial navigation while redirecting to its
            # auth page. The headed context must remain alive for the user to log in.
            pass
        deadline = time.monotonic() + max(1, wait_seconds)
        catalog = None
        last_error = "等待小鹅通登录"
        while time.monotonic() < deadline:
            try:
                # Never navigate while the QR/puzzle page is active: reloading it
                # invalidates the challenge and made the window appear to close.
                cookies = context.cookies()
                logged_in = any(
                    cookie["name"] in {"ctx_user_id", "token", "sessionid"}
                    or "user_id" in cookie["name"] or "login" in cookie["name"]
                    for cookie in cookies
                )
                if logged_in and _authorized(page.url):
                    catalog = _scan_page(page, course_url, 5)
                    break
            except Exception as exc:
                last_error = str(exc)
            page.wait_for_timeout(2_000)
        if catalog is None:
            context.close()
            return {"ok": False, "status": "authorization_required", "session": str(resolved["session"]), "error": last_error}
        atomic_write_json(resolved["catalog"], catalog)
        context.close()
    return {"ok": True, "status": "authorized", "session": str(resolved["session"]), "catalog": str(resolved["catalog"]), "lessons": catalog["lesson_count"]}


def download(config: WorkspaceConfig, course_url: str, chapters: set[int], wait_seconds: int = 12, visible: bool = False) -> dict[str, Any]:
    from convert_voice_to_article import download_video
    from playwright.sync_api import sync_playwright

    resolved = paths(config, course_url)
    resolved["session"].mkdir(parents=True, exist_ok=True)
    resolved["items"].mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(resolved["session"]), headless=False,
            args=["--no-sandbox"] + ([] if visible else ["--start-minimized"]),
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            catalog = _scan_page(page, course_url, 2)
            atomic_write_json(resolved["catalog"], catalog)
            lessons = select_lessons(catalog, chapters)
            if not lessons:
                raise RuntimeError(f"课程目录中没有找到章节：{','.join(map(str, sorted(chapters)))}")
            state = _load_state(resolved["state"], course_url, lessons)
            atomic_write_json(resolved["state"], state)
            completed = skipped = failed = 0
            failures: list[dict[str, str]] = []
            for item in lessons:
                identity = str(item["video_id"])
                package = resolved["items"] / identity
                source_dir = package / "source"
                canonical = source_dir / "source.mp4"
                if _video_ok(canonical):
                    state["items"][identity].update(status="skipped", package=str(package))
                    skipped += 1
                    atomic_write_json(resolved["state"], state)
                    continue
                state["items"][identity].update(status="capturing", package=str(package), error=None)
                atomic_write_json(resolved["state"], state)
                try:
                    candidate = _capture_page(context, item, wait_seconds)
                    if candidate is None:
                        raise RuntimeError("未捕获到 m3u8")
                    state["items"][identity]["status"] = "downloading"
                    atomic_write_json(resolved["state"], state)
                    user_id = str(getattr(candidate, "user_id", "") or "")
                    downloaded = (
                        download_xiaoe_hls(candidate, source_dir, user_id)
                        if user_id else download_video(candidate, source_dir)
                    )
                    if downloaded != canonical:
                        downloaded.replace(canonical)
                    if not _video_ok(canonical):
                        raise RuntimeError("下载结果无法解码")
                    atomic_write_json(source_dir / "metadata.json", {
                        "platform": "xiaoe", "identity": identity, "source_url": item["url"], "title": item["title"],
                    })
                    atomic_write_json(package / "manifest.json", {
                        "schema_version": SCHEMA_VERSION, "platform": "xiaoe", "identity": identity,
                        "title": item["title"], "collection_id": course_id(course_url),
                        "collection_title": catalog["course_title"], "section": item.get("section"), "ordinal": item.get("index"),
                        "artifacts": {"video": "source/source.mp4", "source_video": "source/source.mp4", "metadata": "source/metadata.json"},
                        "stages": {"media_ready": {"result": "passed", "cache_hit": False}},
                    })
                    state["items"][identity].update(status="completed", package=str(package), error=None)
                    completed += 1
                except PermissionError:
                    state["items"][identity].update(status="authorization_required", error="登录已失效")
                    atomic_write_json(resolved["state"], state)
                    raise
                except Exception as exc:
                    state["items"][identity].update(status="failed", error=str(exc))
                    failures.append({"id": identity, "title": item["title"], "error": str(exc)})
                    failed += 1
                finally:
                    atomic_write_json(resolved["state"], state)
        finally:
            context.close()
    return {
        "ok": failed == 0, "status": "complete" if failed == 0 else "partial",
        "course": catalog["course_title"], "chapters": sorted(chapters), "total": len(lessons),
        "completed": completed, "skipped": skipped, "failed": failed, "failures": failures,
        "catalog": str(resolved["catalog"]), "state": str(resolved["state"]), "items_root": str(resolved["items"]),
    }


def status(config: WorkspaceConfig, course_url: str) -> dict[str, Any]:
    resolved = paths(config, course_url)
    if not resolved["state"].is_file():
        return {"ok": True, "status": "not_started", "state": str(resolved["state"])}
    state = json.loads(resolved["state"].read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for item in state.get("items", {}).values():
        name = str(item.get("status") or "unknown")
        counts[name] = counts.get(name, 0) + 1
    return {"ok": True, "status": "active", "counts": counts, "state": str(resolved["state"]), "session": str(resolved["session"])}
