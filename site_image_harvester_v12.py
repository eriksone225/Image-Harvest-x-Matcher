#!/usr/bin/env python3
"""
Site Image Harvester v12

Two-phase workflow:
1) Harvest every image the browser/session can access from a site.
2) Use local_harvest_matcher_v2.py to search the downloaded dataset locally.

This tool does NOT bypass Cloudflare, bot checks, login walls, paywalls, or access controls.
Use a manually verified browser session:
    --headful --pause-first-page
or attach to a Chrome session you verified yourself:
    --connect-cdp http://127.0.0.1:9222

New in v3:
- Adds faster parallel DOM image downloads.
- Adds --network-only, --no-screenshots, --no-html, and --image-concurrency.
- Fixes malformed URL crash: ValueError Invalid IPv6 URL.
- Ignores HTTPS certificate errors by default to handle ERR_CERT_COMMON_NAME_INVALID.
- Opens a verification URL first before numbered page harvesting.
- Press q in the terminal to stop after the current page and save progress.
- Supports numbered pages such as https://pemersatu.store/page/1/ ... /page/373/
  via --page-start and --page-end.
- Keeps saving metadata continuously, so partial runs are usable.
- Saves every image file plus source URL, final URL, found-on page, page title,
  page dates, status, content type, SHA-256, dimensions, and capture method.

Examples:

    python site_image_harvester_v12.py crawl "https://pemersatu.store/" --headful --pause-first-page --page-start 1 --page-end 373 --max-pages 400 --scroll-steps 8 --keep-open

Attach to already verified Chrome:
    & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:TEMP\\forensic_chrome_profile"

    python site_image_harvester_v12.py crawl "https://pemersatu.store/" --connect-cdp http://127.0.0.1:9222 --page-start 1 --page-end 373 --max-pages 400 --scroll-steps 8 --keep-open

HAR fallback:
    python site_image_harvester_v12.py ingest-har "session.har"
"""

from __future__ import annotations

import argparse
import base64
import asyncio
import base64
import csv
import hashlib
import html as html_lib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, urldefrag

from PIL import Image, ImageOps, UnidentifiedImageError


BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".tif", ".tiff")
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv", ".ogv", ".ts", ".m3u8", ".mpd")

def natural_sort_key(text: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text or "")]


def looks_like_ts_segment_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(".ts") or ".ts/" in path or ".ts?" in (url or "").lower()

SKIP_PAGE_EXTENSIONS = (
    ".css", ".js", ".json", ".xml", ".zip", ".rar", ".7z", ".pdf",
    ".mp4", ".webm", ".m3u8", ".mp3", ".wav", ".avi", ".mov", ".wmv",
    ".exe", ".dmg", ".iso"
)
LIKELY_IMAGE_HOST_WORDS = (
    "i0.wp.com", "i1.wp.com", "i2.wp.com", "i3.wp.com",
    "img.", "images.", "cdn.", "static.", "media.",
    "blogger.googleusercontent.com", "blogspot.com", "lulucdn.com"
)
LIKELY_IMAGE_PATH_WORDS = (
    "/image/", "/images/", "/img/", "/thumb/", "/thumbs/", "/thumbnail/",
    "/screenshots/", "/poster/", "/covers/", "/media/", "/upload/",
    "/uploads/", "/wp-content/uploads/"
)
SITEMAP_PATHS = [
    "/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml",
    "/post-sitemap.xml", "/page-sitemap.xml", "/video-sitemap.xml",
    "/category-sitemap.xml", "/tag-sitemap.xml", "/sitemap-posts.xml",
    "/sitemap-pages.xml",
]
DATE_PATTERNS = [
    re.compile(r"\bSubmitted\s*:\s*([^\n\r<]{6,120})", re.IGNORECASE),
    re.compile(r"\bUploaded\s*:\s*([^\n\r<]{6,120})", re.IGNORECASE),
    re.compile(r"\bPublished\s*:\s*([^\n\r<]{6,120})", re.IGNORECASE),
    re.compile(r"\bPosted\s*:\s*([^\n\r<]{6,120})", re.IGNORECASE),
    re.compile(r"\bDate\s*:\s*([^\n\r<]{6,120})", re.IGNORECASE),
    re.compile(r"\b(20\d{2}-\d{2}-\d{2}[T ][0-2]\d:[0-5]\d(?::[0-5]\d)?(?:[+-]\d{2}:?\d{2}|Z)?)\b"),
    re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b"),
    re.compile(r"\b([A-Z][a-z]{2,8}\s+\d{1,2},\s+20\d{2})\b"),
]


@dataclass
class ImageRecord:
    local_path: str
    sha256: str
    source_url: str
    final_url: str
    found_on_page: str
    page_title: str
    page_dates: str
    content_type: str
    status: str
    width: int
    height: int
    file_size_bytes: int
    capture_method: str
    captured_utc: str


@dataclass
class PageRecord:
    page_url: str
    title: str
    extracted_dates: str
    image_urls_seen: int
    image_files_saved_on_page: int
    page_links_seen: int
    saved_html: str
    saved_text: str
    saved_screenshot: str
    blocked_or_verification: bool


@dataclass
class VideoRecord:
    local_path: str
    sha256: str
    source_url: str
    final_url: str
    found_on_page: str
    page_title: str
    page_dates: str
    content_type: str
    status: str
    file_size_bytes: int
    capture_method: str
    captured_utc: str
    note: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    url, _ = urldefrag(url)
    return url


def safe_urljoin(base_url: str, raw_url: str) -> str:
    """
    urljoin/urlparse can crash on malformed strings such as broken IPv6-looking
    fragments found inside scripts. This function filters those out so one bad
    URL cannot kill a 1,641-page harvest.
    """
    raw_url = html_lib.unescape((raw_url or "").strip().strip('"').strip("'"))
    if not raw_url:
        return ""

    lowered = raw_url.lower()
    if lowered.startswith((
        "javascript:", "data:", "mailto:", "tel:", "about:", "chrome:",
        "edge:", "blob:", "filesystem:"
    )):
        return ""

    # Avoid urllib.parse ValueError: Invalid IPv6 URL on malformed bracketed text.
    if raw_url.count("[") != raw_url.count("]"):
        return ""

    try:
        if raw_url.startswith("//"):
            raw_url = "https:" + raw_url
        return clean_url(urljoin(base_url, raw_url))
    except ValueError:
        return ""
    except Exception:
        return ""


def normalize_root(url: str) -> str:
    parsed = urlparse(url if "://" in url else "https://" + url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"


def same_site(url: str, root_netloc: str, include_subdomains: bool = False) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.netloc.lower()
    root = root_netloc.lower()
    return host == root or (include_subdomains and host.endswith("." + root))


def is_probably_page_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower().split("?")[0]
    if parsed.scheme not in {"http", "https"}:
        return False
    if path.endswith(IMAGE_EXTENSIONS):
        return False
    if path.endswith(SKIP_PAGE_EXTENSIONS):
        return False
    return True


def is_probably_image_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.netloc.lower()
    path = parsed.path.lower().split("?")[0]
    if path.endswith(IMAGE_EXTENSIONS):
        return True
    if any(word in host for word in LIKELY_IMAGE_HOST_WORDS):
        return True
    if any(word in parsed.path.lower() for word in LIKELY_IMAGE_PATH_WORDS):
        return True
    return False


def is_probably_video_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    path = parsed.path.lower().split("?")[0]
    if path.endswith(VIDEO_EXTENSIONS):
        return True
    lowered = parsed.path.lower()
    video_markers = (
        "/video/", "/videos/", "/stream/", "/media/", "/uploads/",
        ".mp4", ".webm", ".m3u8", ".mpd", ".m4v", ".mov"
    )
    return any(marker in lowered for marker in video_markers)


def mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_filename(text: str, max_len: int = 100) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "_", text).strip("_")
    return (text[:max_len] or "file").strip("._") or "file"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def suffix_from_url_or_content_type(url: str, content_type: str = "") -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return suffix
    ct = content_type.lower()
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    if "gif" in ct:
        return ".gif"
    if "avif" in ct:
        return ".avif"
    if "bmp" in ct:
        return ".bmp"
    return ".img"


def video_suffix_from_url_or_content_type(url: str, content_type: str = "") -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return suffix
    ct = content_type.lower()
    if "mp4" in ct:
        return ".mp4"
    if "webm" in ct:
        return ".webm"
    if "quicktime" in ct or "mov" in ct:
        return ".mov"
    if "mpegurl" in ct or "m3u8" in ct:
        return ".m3u8"
    if "dash+xml" in ct or "mpd" in ct:
        return ".mpd"
    if "mp2t" in ct:
        return ".ts"
    return ".video"


def load_image_info(data: bytes) -> tuple[int, int]:
    img = Image.open(BytesIO(data))
    img = ImageOps.exif_transpose(img)
    img.load()
    return int(img.size[0]), int(img.size[1])


def extract_dates(text: str) -> list[str]:
    out: list[str] = []
    if not text:
        return out
    for pat in DATE_PATTERNS:
        for match in pat.finditer(text):
            value = re.sub(r"\s+", " ", match.group(1).strip())
            if value and value not in out:
                out.append(value)
    return out[:30]


def html_text_rough(html: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>", " ", html)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def extract_urls_by_regex(base_url: str, text: str) -> set[str]:
    found: set[str] = set()
    patterns = [
        r'https?://[^\s\'"<>]+',
        r'//[^\s\'"<>]+',
        r'(?:src|href|poster|data-src|data-original|data-lazy-src|data-url|data-full)\s*=\s*[\'"]([^\'"]+)[\'"]',
        r'url\(\s*[\'"]?([^\'")]+)[\'"]?\s*\)',
    ]
    for pat in patterns:
        for m in re.finditer(pat, text or "", re.IGNORECASE):
            raw = m.group(1) if m.lastindex else m.group(0)
            full = safe_urljoin(base_url, raw)
            if full:
                found.add(full)
    return found


def is_verification_text(title: str, body_text: str, html: str = "") -> bool:
    combo = f"{title}\n{body_text}\n{html[:4000]}".lower()
    markers = [
        "verifying you are human",
        "checking if the site connection is secure",
        "cloudflare",
        "cf-browser-verification",
        "cf_chl",
        "ray id:",
        "security service to protect against",
    ]
    return any(m in combo for m in markers)


def is_temporary_error_text(title: str, body_text: str, html: str = "") -> bool:
    """
    Detect temporary server/CDN errors where refreshing later may be reasonable.
    This does not treat human-verification pages as refreshable bypass targets.
    """
    combo = f"{title}\n{body_text}\n{html[:6000]}".lower()
    markers = [
        "gateway time-out",
        "gateway timeout",
        "error code 504",
        "504 gateway",
        "bad gateway",
        "error code 502",
        "502 bad gateway",
        "service unavailable",
        "error code 503",
        "503 service unavailable",
        "host error",
        "origin is unreachable",
        "connection timed out",
        "web server reported a gateway time-out",
        "please try again in a few minutes",
    ]
    return any(m in combo for m in markers)


async def get_page_state(page) -> tuple[str, str, str]:
    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        body_text = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        body_text = ""
    try:
        html = await page.content()
    except Exception:
        html = ""
    return title, body_text, html


def is_browser_closed_error(error: Exception | str) -> bool:
    text = str(error).lower()
    return (
        "target page, context or browser has been closed" in text
        or "browser has been closed" in text
        or "target closed" in text
        or "context has been closed" in text
    )


async def recover_temporary_error_page(page, args, page_url: str) -> bool:
    """
    If the page is a temporary Cloudflare/origin error like 504, wait and refresh.
    Returns True when page looks usable or when no temporary error was detected.
    Returns False if all retry attempts still show a temporary error.
    """
    title, body_text, html = await get_page_state(page)

    if is_verification_text(title, body_text, html) and not is_temporary_error_text(title, body_text, html):
        return True

    if not is_temporary_error_text(title, body_text, html):
        return True

    if not args.auto_refresh_errors:
        print("        [temp-error] temporary Cloudflare/server error detected. Auto-refresh is OFF.")
        return False

    print("        [temp-error] temporary Cloudflare/server error detected.")
    for attempt in range(1, args.refresh_retries + 1):
        print(f"        [refresh] retry {attempt}/{args.refresh_retries} after {args.refresh_delay_ms} ms")
        await page.wait_for_timeout(args.refresh_delay_ms)
        try:
            await page.reload(wait_until="domcontentloaded", timeout=args.goto_timeout_ms)
            await page.wait_for_timeout(args.wait_ms)
        except Exception as e:
            if is_browser_closed_error(e):
                raise
            print(f"        [refresh-warn] reload failed: {e}")
            continue

        title, body_text, html = await get_page_state(page)
        if not is_temporary_error_text(title, body_text, html):
            print("        [refresh] page recovered.")
            return True

    print("        [refresh] page still shows temporary error after retries.")
    return False


def write_records_csv(records: list[ImageRecord], path: Path) -> None:
    fields = list(ImageRecord.__dataclass_fields__.keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=fields)
        wr.writeheader()
        for r in records:
            wr.writerow(asdict(r))


def write_pages_csv(records: list[PageRecord], path: Path) -> None:
    fields = list(PageRecord.__dataclass_fields__.keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=fields)
        wr.writeheader()
        for r in records:
            wr.writerow(asdict(r))


def write_video_records_csv(records: list[VideoRecord], path: Path) -> None:
    fields = list(VideoRecord.__dataclass_fields__.keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=fields)
        wr.writeheader()
        for r in records:
            wr.writerow(asdict(r))


def append_jsonl(record: ImageRecord, path: Path) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def terminal_q_pressed() -> bool:
    """
    Non-blocking stop key.
    On Windows, press q in PowerShell/terminal while the crawler is running.
    It stops after the current page and saves progress.
    """
    try:
        import msvcrt  # type: ignore
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            return ch.lower() == "q"
        return False
    except Exception:
        pass

    # Best-effort POSIX fallback.
    try:
        import select
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if ready:
            ch = sys.stdin.read(1)
            return ch.lower() == "q"
    except Exception:
        pass
    return False


class ImageStore:
    def __init__(self, output_dir: Path):
        self.output_dir = mkdir(output_dir)
        self.images_dir = mkdir(output_dir / "images")
        self.metadata_csv = output_dir / "images_metadata.csv"
        self.metadata_jsonl = output_dir / "images_metadata.jsonl"
        self.records: list[ImageRecord] = []
        self.seen_sha: set[str] = set()
        self.seen_source_page: set[tuple[str, str]] = set()

    def save(
        self,
        data: bytes,
        source_url: str,
        final_url: str,
        found_on_page: str,
        page_title: str,
        page_dates: str,
        content_type: str,
        status: str,
        capture_method: str,
    ) -> ImageRecord | None:
        if not data or len(data) < 200:
            return None

        source_url = clean_url(source_url)
        final_url = clean_url(final_url or source_url)
        found_on_page = clean_url(found_on_page)

        key = (final_url or source_url, found_on_page)
        if key in self.seen_source_page:
            return None

        try:
            width, height = load_image_info(data)
        except (UnidentifiedImageError, OSError, ValueError):
            return None

        sha = sha256_bytes(data)
        self.seen_source_page.add(key)

        suffix = suffix_from_url_or_content_type(final_url or source_url, content_type)
        if suffix == ".img":
            suffix = ".jpg"

        filename = f"{sha[:16]}_{width}x{height}{suffix}"
        local_path = self.images_dir / filename

        if sha not in self.seen_sha:
            local_path.write_bytes(data)
            self.seen_sha.add(sha)

        rec = ImageRecord(
            local_path=str(local_path),
            sha256=sha,
            source_url=source_url,
            final_url=final_url,
            found_on_page=found_on_page,
            page_title=page_title,
            page_dates=page_dates,
            content_type=content_type,
            status=str(status),
            width=width,
            height=height,
            file_size_bytes=len(data),
            capture_method=capture_method,
            captured_utc=now_iso(),
        )
        self.records.append(rec)
        append_jsonl(rec, self.metadata_jsonl)
        return rec

    def flush_csv(self) -> None:
        write_records_csv(self.records, self.metadata_csv)



class VideoStore:
    def __init__(self, output_dir: Path):
        self.output_dir = mkdir(output_dir)
        self.videos_dir = mkdir(output_dir / "videos")
        self.metadata_csv = output_dir / "videos_metadata.csv"
        self.metadata_jsonl = output_dir / "videos_metadata.jsonl"
        self.records: list[VideoRecord] = []
        self.seen_sha: set[str] = set()
        self.seen_source_page: set[tuple[str, str]] = set()

    def save(
        self,
        data: bytes,
        source_url: str,
        final_url: str,
        found_on_page: str,
        page_title: str,
        page_dates: str,
        content_type: str,
        status: str,
        capture_method: str,
        note: str = "",
    ) -> VideoRecord | None:
        if not data or len(data) < 32:
            return None

        source_url = clean_url(source_url)
        final_url = clean_url(final_url or source_url)
        found_on_page = clean_url(found_on_page)

        key = (final_url or source_url, found_on_page)
        if key in self.seen_source_page:
            return None

        sha = sha256_bytes(data)
        self.seen_source_page.add(key)

        suffix = video_suffix_from_url_or_content_type(final_url or source_url, content_type)
        if suffix == ".video":
            suffix = ".bin"

        filename = f"{sha[:16]}_{len(data)}bytes{suffix}"
        local_path = self.videos_dir / filename

        if sha not in self.seen_sha:
            local_path.write_bytes(data)
            self.seen_sha.add(sha)

        rec = VideoRecord(
            local_path=str(local_path),
            sha256=sha,
            source_url=source_url,
            final_url=final_url,
            found_on_page=found_on_page,
            page_title=page_title,
            page_dates=page_dates,
            content_type=content_type,
            status=str(status),
            file_size_bytes=len(data),
            capture_method=capture_method,
            captured_utc=now_iso(),
            note=note,
        )
        self.records.append(rec)
        with self.metadata_jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        return rec

    def flush_csv(self) -> None:
        write_video_records_csv(self.records, self.metadata_csv)


async def scroll_page(page, steps: int, delay_ms: int) -> None:
    for _ in range(max(0, steps)):
        try:
            await page.evaluate("() => window.scrollBy(0, Math.max(500, window.innerHeight * 0.85))")
        except Exception:
            pass
        await page.wait_for_timeout(delay_ms)
    try:
        await page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:
        pass
    await page.wait_for_timeout(max(200, delay_ms))


async def wait_for_page_ready(page, args, label: str = "") -> None:
    """
    Extra wait controls before extraction. Useful for JS-heavy and lazy-loaded sites.
    networkidle can hang on sites with analytics/streams, so it is optional.
    """
    if args.wait_load_state:
        try:
            await page.wait_for_load_state(args.wait_load_state, timeout=args.load_state_timeout_ms)
            if label:
                print(f"        [load] {label}: load_state={args.wait_load_state}")
        except Exception as e:
            print(f"        [load-warn] {label}: {args.wait_load_state} wait failed: {e}")

    if args.extra_settle_ms > 0:
        await page.wait_for_timeout(args.extra_settle_ms)


async def scroll_until_bottom(page, args) -> None:
    """
    Scrolls to the bottom repeatedly until document height stops growing.
    This is stronger than fixed scroll steps and helps trigger lazy-loaded images/videos.
    """
    if not args.scroll_until_bottom:
        await scroll_page(page, args.scroll_steps, args.scroll_delay_ms)
        return

    stable_rounds = 0
    last_height = -1
    rounds = max(1, args.max_bottom_scroll_rounds)

    for i in range(rounds):
        try:
            height = await page.evaluate("() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
            y = await page.evaluate("() => window.scrollY + window.innerHeight")
            await page.evaluate("() => window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight))")
            await page.wait_for_timeout(args.bottom_scroll_delay_ms)

            new_height = await page.evaluate("() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
            if abs(new_height - last_height) < 8 and y >= new_height - 8:
                stable_rounds += 1
            else:
                stable_rounds = 0
            last_height = new_height

            if stable_rounds >= args.bottom_stable_rounds:
                print(f"        [lazy-load] bottom reached/stable after {i + 1} rounds")
                break
        except Exception:
            break

    # Give late lazy loaders a final moment to resolve.
    await page.wait_for_timeout(max(200, args.lazy_final_wait_ms))

    if args.return_to_top_after_scroll:
        try:
            await page.evaluate("() => window.scrollTo(0, 0)")
        except Exception:
            pass
        await page.wait_for_timeout(max(200, args.scroll_delay_ms))


async def click_load_more(page, max_clicks: int, delay_ms: int) -> None:
    if max_clicks <= 0:
        return
    words = ["load more", "show more", "next", "older", "lihat lagi", "selanjutnya", "berikutnya"]
    for _ in range(max_clicks):
        clicked = False
        for word in words:
            try:
                loc = page.get_by_text(re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)).first
                if await loc.count() > 0:
                    await loc.click(timeout=1500)
                    clicked = True
                    await page.wait_for_timeout(delay_ms)
                    break
            except Exception:
                continue
        if not clicked:
            break


async def extract_dom(page) -> dict:
    return await page.evaluate(
        r"""() => {
            const imageUrls = new Set();
            const pageLinks = new Set();
            const attrs = [
                "src", "href", "poster", "data-src", "data-original",
                "data-lazy-src", "data-image", "data-url", "data-full",
                "data-thumb", "data-thumbnail", "data-background-image",
                "content"
            ];

            function add(url, bucket) {
                if (!url) return;
                try {
                    const abs = new URL(url, location.href).href;
                    if (bucket === "page") pageLinks.add(abs);
                    else imageUrls.add(abs);
                } catch (e) {}
            }

            function cssUrls(text) {
                const out = [];
                const re = /url\(\s*['"]?([^'")]+)['"]?\s*\)/ig;
                let m;
                while ((m = re.exec(text || "")) !== null) out.push(m[1]);
                return out;
            }

            for (const el of document.querySelectorAll("*")) {
                const tag = el.tagName.toLowerCase();

                for (const attr of attrs) {
                    const val = el.getAttribute(attr);
                    if (!val) continue;
                    if (tag === "a" && attr === "href") add(val, "page");
                    if (tag === "iframe" && attr === "src") add(val, "page");
                    add(val, "image");
                }

                const srcset = el.getAttribute("srcset") || el.getAttribute("data-srcset");
                if (srcset) {
                    for (const part of srcset.split(",")) {
                        const val = part.trim().split(/\s+/)[0];
                        add(val, "image");
                    }
                }

                const style = el.getAttribute("style");
                if (style) for (const val of cssUrls(style)) add(val, "image");
            }

            for (const st of document.querySelectorAll("style")) {
                for (const val of cssUrls(st.textContent || "")) add(val, "image");
            }

            for (const a of document.querySelectorAll("a[href]")) {
                add(a.getAttribute("href"), "page");
                add(a.getAttribute("href"), "image");
            }

            for (const link of document.querySelectorAll("link[href]")) {
                const rel = (link.getAttribute("rel") || "").toLowerCase();
                const as = (link.getAttribute("as") || "").toLowerCase();
                const href = link.getAttribute("href");
                if (as === "image" || rel.includes("image") || rel.includes("preload")) add(href, "image");
                add(href, "page");
            }

            for (const meta of document.querySelectorAll("meta")) {
                const key = (meta.getAttribute("property") || meta.getAttribute("name") || "").toLowerCase();
                const content = meta.getAttribute("content");
                if (content && ["og:image","og:image:url","twitter:image","twitter:image:src"].includes(key)) add(content, "image");
            }

            return {
                title: document.title || "",
                html: document.documentElement.outerHTML || "",
                text: document.body ? document.body.innerText : "",
                images: Array.from(imageUrls),
                links: Array.from(pageLinks)
            };
        }"""
    )


async def force_lazy_media_load(page, args) -> int:
    """
    Forces common lazy-loaded images/backgrounds into real src/srcset attributes so the browser
    itself requests them. This helps sites where the DOM has many data-src/data-original URLs
    but only a small subset actually loads.
    """
    if not args.force_lazy_media:
        return 0
    try:
        changed = await page.evaluate(
            r"""() => {
                let count = 0;
                const lazyAttrs = [
                    "data-src", "data-original", "data-lazy-src", "data-url",
                    "data-full", "data-image", "data-thumb", "data-thumbnail",
                    "data-background-image", "data-bg", "data-poster"
                ];
                const srcsetAttrs = ["data-srcset", "data-lazy-srcset"];

                function absolutize(u) {
                    if (!u) return "";
                    try { return new URL(u, location.href).href; } catch(e) { return ""; }
                }

                for (const img of Array.from(document.querySelectorAll("img"))) {
                    try {
                        img.loading = "eager";
                        img.decoding = "sync";
                        img.removeAttribute("loading");
                        img.removeAttribute("decoding");

                        for (const attr of lazyAttrs) {
                            const v = img.getAttribute(attr);
                            const abs = absolutize(v);
                            if (abs && (!img.getAttribute("src") || img.getAttribute("src").startsWith("data:"))) {
                                img.setAttribute("src", abs);
                                count++;
                                break;
                            }
                        }

                        for (const attr of srcsetAttrs) {
                            const v = img.getAttribute(attr);
                            if (v && !img.getAttribute("srcset")) {
                                img.setAttribute("srcset", v);
                                count++;
                                break;
                            }
                        }
                    } catch(e) {}
                }

                for (const source of Array.from(document.querySelectorAll("source"))) {
                    try {
                        for (const attr of srcsetAttrs) {
                            const v = source.getAttribute(attr);
                            if (v && !source.getAttribute("srcset")) {
                                source.setAttribute("srcset", v);
                                count++;
                                break;
                            }
                        }
                        for (const attr of lazyAttrs) {
                            const v = source.getAttribute(attr);
                            if (v && !source.getAttribute("src")) {
                                source.setAttribute("src", v);
                                count++;
                                break;
                            }
                        }
                    } catch(e) {}
                }

                for (const el of Array.from(document.querySelectorAll("*"))) {
                    try {
                        for (const attr of ["data-background-image", "data-bg", "data-bg-src"]) {
                            const v = el.getAttribute(attr);
                            const abs = absolutize(v);
                            if (abs) {
                                el.style.backgroundImage = `url("${abs}")`;
                                count++;
                                break;
                            }
                        }
                    } catch(e) {}
                }

                try {
                    window.dispatchEvent(new Event("scroll"));
                    window.dispatchEvent(new Event("resize"));
                    document.dispatchEvent(new Event("scroll"));
                } catch(e) {}

                return count;
            }"""
        )
        if changed:
            print(f"        [lazy-force] promoted {changed} lazy media attributes")
            await page.wait_for_timeout(args.lazy_force_wait_ms)
        return int(changed or 0)
    except Exception as e:
        if is_browser_closed_error(e):
            raise
        print(f"        [lazy-force-warn] failed: {e}")
        return 0


async def fetch_image_via_page_context(page, url: str, timeout_ms: int) -> tuple[bytes, str, str]:
    """
    Try downloading from inside the page JS context with credentials included.
    This can work when context.request does not carry the same session/referrer behavior.
    It still obeys browser/CORS rules.
    """
    try:
        result = await page.evaluate(
            r"""async ({url, timeoutMs}) => {
                const ctrl = new AbortController();
                const timer = setTimeout(() => ctrl.abort(), timeoutMs);
                try {
                    const r = await fetch(url, {
                        credentials: "include",
                        cache: "force-cache",
                        redirect: "follow",
                        signal: ctrl.signal,
                        headers: {
                            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
                        }
                    });
                    const ct = r.headers.get("content-type") || "";
                    if (!r.ok) return {ok:false, status:String(r.status), contentType:ct, data:""};
                    const buf = await r.arrayBuffer();
                    let binary = "";
                    const bytes = new Uint8Array(buf);
                    const chunk = 0x8000;
                    for (let i = 0; i < bytes.length; i += chunk) {
                        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                    }
                    return {ok:true, status:String(r.status), contentType:ct, data:btoa(binary)};
                } catch (e) {
                    return {ok:false, status:"page_fetch_error", contentType:"", data:""};
                } finally {
                    clearTimeout(timer);
                }
            }""",
            {"url": url, "timeoutMs": timeout_ms},
        )
        if result and result.get("ok") and result.get("data"):
            return base64.b64decode(result["data"]), result.get("contentType", ""), result.get("status", "200")
    except Exception:
        pass
    return b"", "", ""


async def screenshot_rendered_image_url(page, store: ImageStore, url: str, found_on_page: str, page_title: str, page_dates: str, timeout_ms: int) -> int:
    """
    Last-resort fallback: render the image URL in the browser and screenshot the rendered image.
    This does not preserve the original file bytes, but it captures what the browser can display.
    """
    try:
        img_page = await page.context.new_page()
        await img_page.set_viewport_size({"width": 1600, "height": 1200})
        html = f"""
        <!doctype html>
        <html>
        <body style='margin:0;background:#fff;display:flex;align-items:flex-start;justify-content:flex-start;'>
          <img id='target' src='{html_lib.escape(url, quote=True)}' style='max-width:none;max-height:none;' />
          <script>
            const img = document.getElementById('target');
            img.referrerPolicy = 'no-referrer-when-downgrade';
          </script>
        </body>
        </html>
        """
        await img_page.set_content(html, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            await img_page.locator("#target").wait_for(state="visible", timeout=timeout_ms)
            await img_page.wait_for_function(
                "() => { const img = document.getElementById('target'); return img && img.complete && img.naturalWidth > 0 && img.naturalHeight > 0; }",
                timeout=timeout_ms,
            )
        except Exception:
            await img_page.close()
            return 0

        data = await img_page.locator("#target").screenshot(type="png", timeout=timeout_ms)
        await img_page.close()
        rec = store.save(
            data=data,
            source_url=url,
            final_url=url,
            found_on_page=found_on_page,
            page_title=page_title,
            page_dates=page_dates,
            content_type="image/png",
            status="rendered",
            capture_method="rendered_image_screenshot_fallback",
        )
        return 1 if rec else 0
    except Exception:
        try:
            await img_page.close()
        except Exception:
            pass
        return 0


async def fetch_text_with_context(context, url: str, timeout_ms: int = 25000) -> str | None:
    try:
        r = await context.request.get(url, timeout=timeout_ms, headers={"Accept": "text/xml,text/plain,*/*"})
        if r.status >= 400:
            return None
        return await r.text()
    except Exception:
        return None


def parse_sitemap_text(xml_text: str) -> list[str]:
    urls: list[str] = []
    if not xml_text:
        return urls
    try:
        root = ET.fromstring(xml_text.encode("utf-8", errors="ignore"))
        for elem in root.iter():
            if elem.tag.lower().endswith("loc") and elem.text:
                u = html_lib.unescape(elem.text.strip())
                if u and u not in urls:
                    urls.append(u)
    except Exception:
        pass
    for m in re.finditer(r"<loc>\s*([^<]+?)\s*</loc>", xml_text, re.IGNORECASE):
        u = html_lib.unescape(m.group(1).strip())
        if u and u not in urls:
            urls.append(u)
    return urls


async def discover_sitemap_urls(context, root_url: str, root_netloc: str, include_subdomains: bool, max_urls: int) -> list[str]:
    parsed = urlparse(root_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    queue = deque([base + path for path in SITEMAP_PATHS])
    seen_sitemaps: set[str] = set()
    pages: list[str] = []

    while queue and len(pages) < max_urls:
        sitemap = queue.popleft()
        if sitemap in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap)

        text = await fetch_text_with_context(context, sitemap)
        if not text or "<loc" not in text.lower():
            continue

        locs = parse_sitemap_text(text)
        print(f"[sitemap] {sitemap} -> {len(locs)} loc entries")

        for loc in locs:
            loc = clean_url(loc)
            if not loc or not same_site(loc, root_netloc, include_subdomains):
                continue
            if loc.lower().split("?")[0].endswith(".xml"):
                if loc not in seen_sitemaps:
                    queue.append(loc)
            elif is_probably_page_url(loc) and loc not in pages:
                pages.append(loc)
                if len(pages) >= max_urls:
                    break

    return pages[:max_urls]


def build_numbered_page_urls(root_url: str, start: int, end: int, template: str = "") -> list[str]:
    if start <= 0 or end <= 0:
        return []
    if end < start:
        start, end = end, start

    parsed = urlparse(normalize_root(root_url))
    base = f"{parsed.scheme}://{parsed.netloc}"

    if template:
        # Example: "https://pemersatu.store/page/{n}/"
        return [template.format(n=i) for i in range(start, end + 1)]

    return [f"{base}/page/{i}/" for i in range(start, end + 1)]


def split_filter_terms(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def passes_url_filters(url: str, include_terms: list[str], exclude_terms: list[str]) -> bool:
    """
    Optional crawl filters.
    include_terms: if present, at least one term must appear in URL.
    exclude_terms: if any term appears, URL is rejected.
    """
    lowered = url.lower()
    include_terms = [x.lower() for x in include_terms]
    exclude_terms = [x.lower() for x in exclude_terms]
    if include_terms and not any(term in lowered for term in include_terms):
        return False
    if exclude_terms and any(term in lowered for term in exclude_terms):
        return False
    return True


async def wait_for_manual_verification_if_needed(page, args, forced: bool = False) -> bool:
    try:
        title = await page.title()
        body = await page.locator("body").inner_text(timeout=5000)
        html = await page.content()
    except Exception:
        title, body, html = "", "", ""

    blocked = is_verification_text(title, body, html)

    if forced or blocked:
        if not args.headful and not args.connect_cdp:
            print("[blocked] Verification page detected in headless mode.")
            print("          Use --headful --pause-first-page or --connect-cdp with verified Chrome.")
            return False

        while True:
            print("\n[manual] Complete verification/navigation in the browser.")
            print("[manual] When the real page is visible, press Enter here. Type q then Enter to stop.")
            ans = input("> ").strip().lower()
            if ans == "q":
                return False
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(args.wait_ms)
            try:
                title = await page.title()
                body = await page.locator("body").inner_text(timeout=5000)
                html = await page.content()
            except Exception:
                title, body, html = "", "", ""
            if not is_verification_text(title, body, html):
                return True

    return True


async def save_dom_image_urls(context, store: ImageStore, urls: Iterable[str], found_on_page: str, page_title: str, page_dates: str, timeout_ms: int, concurrency: int = 8, page=None, args=None) -> int:
    """
    Download DOM-discovered image URLs with multiple strategies:
    1) Playwright context.request with browser-like headers.
    2) Optional in-page fetch(credentials='include') fallback.
    3) Optional rendered image screenshot fallback.
    """
    url_list = []
    seen = set()
    for url in urls:
        url = clean_url(url)
        if not url or not is_probably_image_url(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        url_list.append(url)

    if not url_list:
        return 0

    saved = 0
    context_ok = 0
    page_fetch_ok = 0
    screenshot_ok = 0
    sem = asyncio.Semaphore(max(1, concurrency))

    try:
        user_agent = await page.evaluate("() => navigator.userAgent") if page else ""
    except Exception:
        user_agent = ""

    parsed_page = urlparse(found_on_page)
    origin = f"{parsed_page.scheme}://{parsed_page.netloc}" if parsed_page.scheme and parsed_page.netloc else ""

    async def fetch_one(url: str) -> tuple[int, str]:
        async with sem:
            # Strategy 1: request API
            try:
                headers = {
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    "Referer": found_on_page,
                    "Origin": origin,
                    "Sec-Fetch-Dest": "image",
                    "Sec-Fetch-Mode": "no-cors",
                    "Sec-Fetch-Site": "cross-site" if urlparse(url).netloc != parsed_page.netloc else "same-origin",
                }
                if user_agent:
                    headers["User-Agent"] = user_agent
                r = await context.request.get(url, timeout=timeout_ms, headers=headers)
                if r.status < 400:
                    data = await r.body()
                    rec = store.save(
                        data=data,
                        source_url=url,
                        final_url=url,
                        found_on_page=found_on_page,
                        page_title=page_title,
                        page_dates=page_dates,
                        content_type=r.headers.get("content-type", ""),
                        status=str(r.status),
                        capture_method="dom_url_context_request_headers",
                    )
                    if rec:
                        return 1, "context"
            except Exception:
                pass

            # Strategy 2: fetch inside the page context, with credentials.
            if page is not None and args is not None and args.page_fetch_images:
                data, ct, status = await fetch_image_via_page_context(page, url, timeout_ms)
                if data:
                    rec = store.save(
                        data=data,
                        source_url=url,
                        final_url=url,
                        found_on_page=found_on_page,
                        page_title=page_title,
                        page_dates=page_dates,
                        content_type=ct,
                        status=status or "200",
                        capture_method="page_context_fetch_credentials",
                    )
                    if rec:
                        return 1, "page_fetch"

            # Strategy 3: screenshot what the browser can render.
            if page is not None and args is not None and args.screenshot_image_fallback:
                ok = await screenshot_rendered_image_url(page, store, url, found_on_page, page_title, page_dates, timeout_ms)
                if ok:
                    return 1, "screenshot"

            return 0, "failed"

    results = await asyncio.gather(*(fetch_one(u) for u in url_list), return_exceptions=True)
    for r in results:
        if isinstance(r, tuple):
            n, method = r
            saved += n
            if n:
                if method == "context":
                    context_ok += 1
                elif method == "page_fetch":
                    page_fetch_ok += 1
                elif method == "screenshot":
                    screenshot_ok += 1

    failed = max(0, len(url_list) - saved)
    if url_list:
        print(f"        [image-dom] urls={len(url_list)} saved={saved} context={context_ok} page_fetch={page_fetch_ok} screenshots={screenshot_ok} failed={failed}")
    if url_list and failed and saved == 0:
        print(f"        [image-dom-warn] DOM image URLs found but none downloaded by any enabled strategy. Network capture may still save loaded images.")
    return saved


async def save_dom_video_urls(context, store: VideoStore, urls: Iterable[str], found_on_page: str, page_title: str, page_dates: str, timeout_ms: int, concurrency: int = 4, max_video_mb: int = 100, args=None) -> int:
    """
    Download direct video/media URLs discovered in DOM/page source.
    For HLS/DASH, this saves the manifest (.m3u8/.mpd), not the segmented stream.
    It does not bypass DRM, paywalls, private accounts, or anti-bot controls.
    """
    url_list = []
    seen = set()
    for url in urls:
        url = clean_url(url)
        if not url or not is_probably_video_url(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        url_list.append(url)

    if not url_list:
        return 0

    saved = 0
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, concurrency))
    max_bytes = max(1, max_video_mb) * 1024 * 1024

    async def fetch_one(url: str) -> int:
        async with sem:
            try:
                r = await context.request.get(
                    url,
                    timeout=timeout_ms,
                    headers={
                        "Accept": "video/mp4,video/webm,application/vnd.apple.mpegurl,application/dash+xml,video/*,*/*;q=0.8",
                        "Referer": found_on_page,
                    },
                )
                if r.status >= 400:
                    return 0

                content_length = r.headers.get("content-length") or ""
                try:
                    if content_length and int(content_length) > max_bytes:
                        print(f"        [video-skip] too large by content-length: {url}")
                        return 0
                except Exception:
                    pass

                data = await r.body()
                if len(data) > max_bytes:
                    print(f"        [video-skip] too large after download: {url}")
                    return 0

                ct = r.headers.get("content-type", "")
                note = ""
                if "mpegurl" in ct.lower() or url.lower().split("?")[0].endswith(".m3u8"):
                    note = "HLS manifest saved; segmented stream not stitched."
                elif "dash+xml" in ct.lower() or url.lower().split("?")[0].endswith(".mpd"):
                    note = "DASH manifest saved; segmented stream not stitched."

                async with lock:
                    rec = store.save(
                        data=data,
                        source_url=url,
                        final_url=url,
                        found_on_page=found_on_page,
                        page_title=page_title,
                        page_dates=page_dates,
                        content_type=ct,
                        status=str(r.status),
                        capture_method="dom_video_url_context_request_parallel",
                        note=note,
                    )

                count = 1 if rec else 0
                if args is not None and args.download_hls_segments and url.lower().split("?")[0].endswith(".m3u8"):
                    count += await download_hls_playlist(context, store, url, found_on_page, page_title, page_dates, args)
                return count
            except Exception:
                return 0

    results = await asyncio.gather(*(fetch_one(u) for u in url_list), return_exceptions=True)
    for r in results:
        if isinstance(r, int):
            saved += r
    return saved


def parse_m3u8_lines(text: str) -> tuple[bool, bool, list[tuple[int, str]], list[str]]:
    """
    Returns:
      encrypted: EXT-X-KEY with METHOD not NONE
      is_master: playlist has variant STREAM-INF entries
      variants: list of (bandwidth, uri)
      segments: list of segment URIs
    """
    encrypted = False
    variants = []
    segments = []
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]

    for i, ln in enumerate(lines):
        upper = ln.upper()
        if upper.startswith("#EXT-X-KEY") and "METHOD=NONE" not in upper:
            encrypted = True

        if upper.startswith("#EXT-X-STREAM-INF"):
            bw = 0
            m = re.search(r"BANDWIDTH=(\d+)", ln, re.I)
            if m:
                try:
                    bw = int(m.group(1))
                except Exception:
                    bw = 0
            # The next non-comment line is normally the variant URI.
            for nxt in lines[i + 1:]:
                if not nxt.startswith("#"):
                    variants.append((bw, nxt))
                    break

    is_master = bool(variants)

    if not is_master:
        for ln in lines:
            if ln.startswith("#"):
                continue
            # Ignore nested playlists here; direct segment/media lines only.
            segments.append(ln)

    return encrypted, is_master, variants, segments


async def download_hls_playlist(context, store: VideoStore, playlist_url: str, found_on_page: str, page_title: str, page_dates: str, args) -> int:
    """
    Best-effort HLS downloader. It downloads accessible .ts/.m4s segments from a plain
    non-encrypted media playlist and concatenates them into one .ts file.

    It does not decrypt, bypass DRM, or stitch DASH. If it sees EXT-X-KEY encryption,
    it saves/skips safely.
    """
    if not args.download_hls_segments:
        return 0

    playlist_url = clean_url(playlist_url)
    if not playlist_url.lower().split("?")[0].endswith(".m3u8"):
        return 0

    try:
        r = await context.request.get(
            playlist_url,
            timeout=args.video_timeout_ms,
            headers={"Accept": "application/vnd.apple.mpegurl,*/*", "Referer": found_on_page},
        )
        if r.status >= 400:
            print(f"        [hls-skip] manifest HTTP {r.status}: {playlist_url}")
            return 0
        text = (await r.body()).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"        [hls-skip] failed manifest fetch: {playlist_url} :: {e}")
        return 0

    encrypted, is_master, variants, segments = parse_m3u8_lines(text)

    if encrypted:
        print(f"        [hls-skip] encrypted HLS playlist detected, not downloading segments: {playlist_url}")
        return 0

    if is_master and variants:
        # Pick the highest bandwidth variant by default.
        variants.sort(key=lambda x: x[0], reverse=True)
        chosen = variants[0][1]
        child_url = safe_urljoin(playlist_url, chosen)
        if child_url and child_url != playlist_url:
            print(f"        [hls] master playlist -> selected variant bandwidth={variants[0][0]}")
            return await download_hls_playlist(context, store, child_url, found_on_page, page_title, page_dates, args)
        return 0

    if not segments:
        print(f"        [hls-skip] no segments found: {playlist_url}")
        return 0

    # Limit segments to avoid accidental huge downloads.
    if args.hls_max_segments > 0 and len(segments) > args.hls_max_segments:
        print(f"        [hls] limiting segments {len(segments)} -> {args.hls_max_segments}")
        segments = segments[:args.hls_max_segments]

    segment_urls = []
    for seg in segments:
        u = safe_urljoin(playlist_url, seg)
        if u:
            segment_urls.append(u)

    if not segment_urls:
        return 0

    max_total = max(1, args.max_video_mb) * 1024 * 1024
    sem = asyncio.Semaphore(max(1, args.hls_concurrency))

    async def fetch_segment(idx_url):
        idx, url = idx_url
        async with sem:
            try:
                rr = await context.request.get(
                    url,
                    timeout=args.video_timeout_ms,
                    headers={"Accept": "video/mp2t,video/*,*/*", "Referer": found_on_page},
                )
                if rr.status >= 400:
                    return idx, b"", f"HTTP {rr.status}"
                data = await rr.body()
                return idx, data, ""
            except Exception as e:
                return idx, b"", str(e)

    print(f"        [hls] downloading {len(segment_urls)} segments")
    results = await asyncio.gather(*(fetch_segment(x) for x in enumerate(segment_urls)), return_exceptions=True)

    ordered = []
    failures = 0
    total = 0
    for result in results:
        if isinstance(result, Exception):
            failures += 1
            continue
        idx, data, err = result
        if err or not data:
            failures += 1
            continue
        total += len(data)
        if total > max_total:
            print(f"        [hls-skip] total HLS size exceeded max_video_mb={args.max_video_mb}")
            return 0
        ordered.append((idx, data))

    ordered.sort(key=lambda x: x[0])
    if not ordered:
        return 0

    merged = b"".join(data for _idx, data in ordered)
    note = f"HLS segments concatenated into .ts; segments={len(ordered)}/{len(segment_urls)}; failures={failures}; source_manifest={playlist_url}"
    rec = store.save(
        data=merged,
        source_url=playlist_url,
        final_url=playlist_url,
        found_on_page=found_on_page,
        page_title=page_title,
        page_dates=page_dates,
        content_type="video/mp2t",
        status="200",
        capture_method="hls_segment_concat",
        note=note,
    )
    if rec:
        print(f"        [hls] saved concatenated TS from {len(ordered)} segments")
        return 1
    return 0


async def download_ts_url_sequence(context, store: VideoStore, urls: Iterable[str], found_on_page: str, page_title: str, page_dates: str, args) -> int:
    """
    Fallback for sites that expose .ts segment URLs in network logs but not a clean .m3u8.
    It sorts segment URLs naturally and concatenates accessible responses.
    This is best-effort and may not produce a playable video if the observed segments are incomplete,
    encrypted, discontinuous, or only byte-range fragments.
    """
    if not args.concat_detected_ts_segments:
        return 0

    unique = []
    seen = set()
    for u in urls:
        u = clean_url(u)
        if not u or u in seen or not looks_like_ts_segment_url(u):
            continue
        seen.add(u)
        unique.append(u)

    if len(unique) < 2:
        return 0

    unique.sort(key=natural_sort_key)
    if args.hls_max_segments > 0 and len(unique) > args.hls_max_segments:
        print(f"        [ts-concat] limiting detected TS segments {len(unique)} -> {args.hls_max_segments}")
        unique = unique[:args.hls_max_segments]

    max_total = max(1, args.max_video_mb) * 1024 * 1024
    sem = asyncio.Semaphore(max(1, args.hls_concurrency))

    async def fetch_one(idx_url):
        idx, url = idx_url
        async with sem:
            try:
                rr = await context.request.get(
                    url,
                    timeout=args.video_timeout_ms,
                    headers={"Accept": "video/mp2t,video/*,*/*", "Referer": found_on_page},
                )
                if rr.status >= 400:
                    return idx, b"", f"HTTP {rr.status}"
                data = await rr.body()
                return idx, data, ""
            except Exception as e:
                return idx, b"", str(e)

    print(f"        [ts-concat] downloading {len(unique)} observed TS segment URLs")
    results = await asyncio.gather(*(fetch_one(x) for x in enumerate(unique)), return_exceptions=True)
    ordered = []
    failures = 0
    total = 0
    for result in results:
        if isinstance(result, Exception):
            failures += 1
            continue
        idx, data, err = result
        if err or not data:
            failures += 1
            continue
        total += len(data)
        if total > max_total:
            print(f"        [ts-concat-skip] total size exceeded max_video_mb={args.max_video_mb}")
            return 0
        ordered.append((idx, data))

    ordered.sort(key=lambda x: x[0])
    if len(ordered) < 2:
        return 0

    merged = b"".join(data for _idx, data in ordered)
    note = f"Observed TS URLs concatenated best-effort; segments={len(ordered)}/{len(unique)}; failures={failures}; may be incomplete if page did not load all segments."
    rec = store.save(
        data=merged,
        source_url=unique[0],
        final_url=unique[0],
        found_on_page=found_on_page,
        page_title=page_title,
        page_dates=page_dates,
        content_type="video/mp2t",
        status="200",
        capture_method="observed_ts_segment_concat",
        note=note,
    )
    if rec:
        print(f"        [ts-concat] saved best-effort concatenated TS from observed segments")
        return 1
    return 0


ADBLOCK_DOMAIN_PARTS = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "adservice.google.", "pagead2.googlesyndication.com", "adnxs.com",
    "adsystem.com", "adform.net", "taboola.com", "outbrain.com",
    "popads.net", "popcash.net", "propellerads.com", "trafficjunky.net",
    "exoclick.com", "juicyads.com", "mgid.com", "revcontent.com",
    "sharethrough.com", "amazon-adsystem.com", "facebook.net/tr",
    "analytics.google.com", "google-analytics.com", "googletagmanager.com",
    "hotjar.com", "scorecardresearch.com", "quantserve.com", "moatads.com",
)

ADBLOCK_URL_PARTS = (
    "/ads/", "/adserver/", "/advert/", "/advertising/", "/banners/",
    "/banner/", "popunder", "popup", "prebid", "bidder", "analytics",
    "tracker", "tracking", "telemetry",
)


def should_block_request_url(url: str, resource_type: str, harvest_videos: bool = False) -> bool:
    lowered = (url or "").lower()
    rtype = (resource_type or "").lower()
    if rtype == "media" and harvest_videos:
        return False
    if rtype in {"beacon", "eventsource"}:
        return True
    if rtype == "font":
        return True
    if any(part in lowered for part in ADBLOCK_DOMAIN_PARTS):
        return True
    if any(part in lowered for part in ADBLOCK_URL_PARTS):
        return True
    return False


async def install_adblock_routes(context, args) -> None:
    if not args.block_ads:
        return

    async def route_handler(route):
        try:
            request = route.request
            if should_block_request_url(request.url, request.resource_type, harvest_videos=args.harvest_videos):
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    try:
        await context.route("**/*", route_handler)
        print("[adblock] request blocking enabled")
    except Exception as e:
        print(f"[adblock-warn] could not install route blocker: {e}")


async def setup_popup_closer(page, args) -> None:
    if not args.close_popups:
        return

    def on_popup(popup):
        async def closer():
            try:
                print(f"        [popup] closing popup tab: {popup.url}")
                await popup.close()
            except Exception:
                pass
        asyncio.create_task(closer())

    try:
        page.on("popup", on_popup)
    except Exception:
        pass


async def cleanup_page_popups(page, args) -> int:
    if not args.close_popups:
        return 0

    try:
        removed = await page.evaluate(
            r"""() => {
                let count = 0;

                const closeSelectors = [
                    'button[aria-label*="close" i]', 'button[title*="close" i]',
                    '[role="button"][aria-label*="close" i]',
                    '.close', '.close-button', '.btn-close', '.modal-close',
                    '.popup-close', '.ad-close', '.ads-close', '.overlay-close',
                    '.mfp-close', '.fancybox-close', '.fancybox-button--close',
                    '#close', '#popup-close', '[class*="close" i]', '[id*="close" i]'
                ];

                for (const sel of closeSelectors) {
                    for (const el of Array.from(document.querySelectorAll(sel)).slice(0, 25)) {
                        try {
                            const rect = el.getBoundingClientRect();
                            const style = getComputedStyle(el);
                            if (rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none') {
                                el.click();
                                count++;
                            }
                        } catch (e) {}
                    }
                }

                const removeSelectors = [
                    '[id*="ad" i]', '[class*="ad-" i]', '[class*="_ad" i]',
                    '[class*="ads" i]', '[id*="ads" i]',
                    '[class*="advert" i]', '[id*="advert" i]',
                    '[class*="banner" i]', '[id*="banner" i]',
                    '[class*="sponsor" i]', '[id*="sponsor" i]',
                    '[class*="popup" i]', '[id*="popup" i]',
                    '[class*="popunder" i]', '[id*="popunder" i]',
                    '[class*="interstitial" i]', '[id*="interstitial" i]',
                    '[class*="cookie" i]', '[id*="cookie" i]',
                    'iframe[src*="ads" i]', 'iframe[src*="doubleclick" i]'
                ];

                for (const sel of removeSelectors) {
                    for (const el of Array.from(document.querySelectorAll(sel)).slice(0, 140)) {
                        try {
                            const rect = el.getBoundingClientRect();
                            const style = getComputedStyle(el);
                            const fixedish = style.position === 'fixed' || style.position === 'sticky';
                            const largeOverlay = rect.width > innerWidth * 0.40 && rect.height > innerHeight * 0.16;
                            const visibleAd = rect.width > 80 && rect.height > 40;
                            if ((fixedish && visibleAd) || largeOverlay) {
                                el.remove();
                                count++;
                            }
                        } catch (e) {}
                    }
                }

                try {
                    document.documentElement.style.overflow = 'auto';
                    document.body.style.overflow = 'auto';
                    document.body.style.position = '';
                } catch (e) {}

                return count;
            }"""
        )
        if removed:
            print(f"        [popup-cleanup] removed/clicked {removed} popup/ad elements")
        return int(removed or 0)
    except Exception as e:
        if is_browser_closed_error(e):
            raise
        print(f"        [popup-cleanup-warn] failed: {e}")
        return 0


async def probe_video_playback(page, args) -> set[str]:
    """
    Best-effort video probing:
    - finds <video> and <source> tags
    - sets videos muted/preload so browser policy is less likely to block play()
    - calls play() on visible/available video elements
    - waits briefly so the page can resolve currentSrc/network URLs
    - reads currentSrc/src/source tags and performance resource entries

    This only reveals media that the page/browser session is already allowed to access.
    It does not bypass DRM, login walls, paywalls, or anti-bot controls.
    """
    if not args.harvest_videos or not args.probe_video_playback:
        return set()

    try:
        urls = await page.evaluate(
            r"""async ({maxVideos, waitMs}) => {
                const out = new Set();

                function add(u) {
                    if (!u) return;
                    try {
                        const abs = new URL(u, location.href).href;
                        if (!abs.startsWith("blob:") && !abs.startsWith("data:")) out.add(abs);
                    } catch (e) {}
                }

                const videos = Array.from(document.querySelectorAll("video")).slice(0, maxVideos);
                for (const v of videos) {
                    try {
                        v.muted = true;
                        v.playsInline = true;
                        v.preload = "auto";
                        add(v.currentSrc);
                        add(v.src);
                        for (const s of Array.from(v.querySelectorAll("source"))) add(s.src || s.getAttribute("src"));
                        const p = v.play();
                        if (p && typeof p.catch === "function") await p.catch(() => {});
                    } catch (e) {}
                }

                await new Promise(resolve => setTimeout(resolve, waitMs));

                for (const v of videos) {
                    try {
                        add(v.currentSrc);
                        add(v.src);
                        for (const s of Array.from(v.querySelectorAll("source"))) add(s.src || s.getAttribute("src"));
                        try { v.pause(); } catch (e) {}
                    } catch (e) {}
                }

                try {
                    for (const entry of performance.getEntriesByType("resource")) {
                        const name = entry.name || "";
                        if (/\.(mp4|webm|mov|m4v|m3u8|mpd|ts)(\?|#|$)/i.test(name) ||
                            /video|media|stream|m3u8|mpd/i.test(name)) {
                            add(name);
                        }
                    }
                } catch (e) {}

                return Array.from(out);
            }""",
            {
                "maxVideos": int(args.max_video_elements),
                "waitMs": int(args.video_probe_seconds * 1000),
            },
        )
        found = set()
        for u in urls or []:
            full = safe_urljoin(page.url, u)
            if full and is_probably_video_url(full):
                found.add(full)
        if found:
            print(f"        [video-probe] discovered {len(found)} possible media URLs by playing/probing video elements")
        return found
    except Exception as e:
        if is_browser_closed_error(e):
            raise
        print(f"        [video-probe-warn] failed: {e}")
        return set()


async def crawl(args: argparse.Namespace) -> None:
    try:
        from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
    except Exception:
        raise SystemExit("Playwright not installed. Run: pip install playwright && python -m playwright install chromium")

    out = mkdir(Path(args.output))
    evidence = mkdir(out / "evidence")
    html_dir = mkdir(evidence / "page_html")
    text_dir = mkdir(evidence / "page_text")
    shots_dir = mkdir(evidence / "page_screenshots")

    store = ImageStore(out)
    video_store = VideoStore(out)
    pages: list[PageRecord] = []

    root_url = normalize_root(args.url)
    root_netloc = urlparse(root_url).netloc

    include_terms = split_filter_terms(args.url_include)
    exclude_terms = split_filter_terms(args.url_exclude)

    crawl_mode = args.crawl_mode
    if crawl_mode == "auto":
        if args.page_start > 0 and args.page_end > 0:
            crawl_mode = "numbered"
        elif args.discover_sitemaps:
            crawl_mode = "sitemap"
        else:
            crawl_mode = "link"

    numbered_pages = []
    if crawl_mode == "numbered":
        numbered_pages = build_numbered_page_urls(root_url, args.page_start, args.page_end, args.page_template)

    if numbered_pages:
        numbered_pages = [u for u in numbered_pages if passes_url_filters(u, include_terms, exclude_terms)]
        queue: deque[str] = deque(numbered_pages)
        queued: set[str] = set(numbered_pages)
        print(f"[crawl-mode] numbered")
        print(f"[numbered-pages] queued {len(numbered_pages)} pages")
        if numbered_pages:
            print(f"[numbered-pages] from {numbered_pages[0]} to {numbered_pages[-1]}")
    else:
        queue = deque([root_url])
        queued = {root_url}
        print(f"[crawl-mode] {crawl_mode}")

    seen_pages: set[str] = set()
    current = {"url": root_url, "title": "", "dates": ""}
    network_video_urls_by_page: dict[str, set[str]] = {}

    print("[stop] While harvesting, press q in this terminal to stop after the current page and save progress.")

    async with async_playwright() as p:
        browser = None
        context = None
        should_close_context = False

        if args.connect_cdp:
            print(f"[browser] connecting to existing Chrome: {args.connect_cdp}")
            try:
                browser = await p.chromium.connect_over_cdp(args.connect_cdp)
            except Exception as e:
                print(f"[fatal] Could not connect to Chrome CDP at {args.connect_cdp}: {e}")
                print("[hint] In the GUI, click 'Open Verification Chrome' first, or enable auto-open CDP Chrome.")
                print("[hint] The error ECONNREFUSED means no Chrome is listening on port 9222.")
                return
            if not browser.contexts:
                raise SystemExit("Connected to Chrome but no browser context was found.")
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
        else:
            launch_kwargs = {
                "headless": not args.headful,
                "user_agent": BROWSER_UA,
                "viewport": {"width": args.viewport_width, "height": args.viewport_height},
                "accept_downloads": False,
                "ignore_https_errors": not args.strict_https,
            }
            if args.channel:
                launch_kwargs["channel"] = args.channel
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(Path(args.user_data_dir).resolve()),
                **launch_kwargs,
            )
            should_close_context = True
            page = await context.new_page()

        async def handle_response(response):
            try:
                url = clean_url(response.url)
                ct = (response.headers.get("content-type") or "").lower()

                if "image/" in ct or is_probably_image_url(url):
                    body = await response.body()
                    if not body or len(body) > args.max_image_mb * 1024 * 1024:
                        return
                    store.save(
                        data=body,
                        source_url=url,
                        final_url=url,
                        found_on_page=current["url"],
                        page_title=current["title"],
                        page_dates=current["dates"],
                        content_type=ct,
                        status=str(response.status),
                        capture_method="browser_network_response",
                    )
                    return

                if args.harvest_videos and ("video/" in ct or "mpegurl" in ct or "dash+xml" in ct or is_probably_video_url(url)):
                    try:
                        network_video_urls_by_page.setdefault(current["url"], set()).add(url)
                    except Exception:
                        pass

                    # Avoid saving partial byte-range chunks as standalone videos, but keep the URL
                    # so the page-level downloader can re-request full segments or concatenate observed .ts.
                    if str(response.status) == "206":
                        return
                    clen = response.headers.get("content-length") or ""
                    try:
                        if clen and int(clen) > args.max_video_mb * 1024 * 1024:
                            return
                    except Exception:
                        pass
                    body = await response.body()
                    if not body or len(body) > args.max_video_mb * 1024 * 1024:
                        return
                    note = ""
                    if "mpegurl" in ct or url.lower().split("?")[0].endswith(".m3u8"):
                        note = "HLS manifest saved; segmented stream not stitched."
                    elif "dash+xml" in ct or url.lower().split("?")[0].endswith(".mpd"):
                        note = "DASH manifest saved; segmented stream not stitched."
                    video_store.save(
                        data=body,
                        source_url=url,
                        final_url=url,
                        found_on_page=current["url"],
                        page_title=current["title"],
                        page_dates=current["dates"],
                        content_type=ct,
                        status=str(response.status),
                        capture_method="browser_network_video_response",
                        note=note,
                    )
                    return
            except Exception:
                return

        await install_adblock_routes(context, args)
        await setup_popup_closer(page, args)
        page.on("response", lambda response: asyncio.create_task(handle_response(response)))

        first = True
        stop_requested = False

        # Important for numbered page harvesting:
        # open a stable verification URL first, let the user complete Cloudflare once,
        # then continue to /page/1/, /page/2/, etc. This prevents losing early pages
        # and prevents the browser from closing just because the first numbered page
        # hits a certificate or verification interstitial.
        if args.pause_first_page:
            verify_url = clean_url(args.verify_url or root_url)
            current["url"] = verify_url
            print(f"[verify] opening verification page first: {verify_url}")
            try:
                await page.goto(verify_url, wait_until="domcontentloaded", timeout=args.goto_timeout_ms)
                await page.wait_for_timeout(args.wait_ms)
            except PlaywrightTimeoutError:
                print("        [warn] verification page navigation timeout; use the browser window manually.")
            except Exception as e:
                print(f"        [warn] verification page navigation failed: {e}")
                print("        You can still use the opened browser manually, then press Enter at the prompt.")
            if not await wait_for_manual_verification_if_needed(page, args, forced=True):
                stop_requested = True
            first = False

        while queue and len(seen_pages) < args.max_pages and not stop_requested:
            if terminal_q_pressed():
                print("[stop] q pressed. Saving progress and stopping before next page.")
                stop_requested = True
                break

            page_url = clean_url(queue.popleft())

            if page_url in seen_pages:
                continue
            if not same_site(page_url, root_netloc, args.include_subdomains):
                continue
            if not is_probably_page_url(page_url):
                continue

            seen_pages.add(page_url)
            current["url"] = page_url
            current["title"] = ""
            current["dates"] = ""

            print(f"[crawl] {len(seen_pages):>5}/{args.max_pages} {page_url}")

            try:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=args.goto_timeout_ms)
                await page.wait_for_timeout(args.wait_ms)
            except PlaywrightTimeoutError:
                print("        [warn] navigation timeout; extracting whatever loaded.")
            except Exception as e:
                print(f"        [warn] navigation failed: {e}")
                if is_browser_closed_error(e):
                    print("        [fatal] browser/tab/context closed. Stopping harvest instead of burning through remaining pages.")
                    stop_requested = True
                    break
                continue

            try:
                await cleanup_page_popups(page, args)
            except Exception as e:
                print(f"        [warn] popup cleanup failed after navigation: {e}")
                if is_browser_closed_error(e):
                    stop_requested = True
                    break

            if first and args.pause_first_page:
                if not await wait_for_manual_verification_if_needed(page, args, forced=True):
                    break
            first = False

            if not await wait_for_manual_verification_if_needed(page, args, forced=False):
                pages.append(PageRecord(page_url, "", "", 0, 0, 0, "", "", "", True))
                if args.stop_on_block:
                    break
                continue

            try:
                recovered = await recover_temporary_error_page(page, args, page_url)
            except Exception as e:
                print(f"        [warn] recovery failed: {e}")
                if is_browser_closed_error(e):
                    print("        [fatal] browser/tab/context closed during recovery. Stopping harvest.")
                    stop_requested = True
                    break
                recovered = False

            if not recovered:
                pages.append(PageRecord(page_url, "", "", 0, 0, 0, "", "", "", True))
                if args.stop_on_block:
                    break
                continue

            if crawl_mode in {"sitemap", "auto_sitemap"} and len(seen_pages) == 1:
                sitemap_pages = await discover_sitemap_urls(context, root_url, root_netloc, args.include_subdomains, args.max_sitemap_urls)
                added = 0
                for u in sitemap_pages:
                    if not passes_url_filters(u, include_terms, exclude_terms):
                        continue
                    if u not in seen_pages and u not in queued:
                        queue.append(u)
                        queued.add(u)
                        added += 1
                print(f"[sitemap] enqueued {added} filtered pages")

            probed_video_urls = set()
            try:
                await wait_for_page_ready(page, args, label="before-scroll")
                await cleanup_page_popups(page, args)
                await force_lazy_media_load(page, args)
                await scroll_until_bottom(page, args)
                await force_lazy_media_load(page, args)
                await cleanup_page_popups(page, args)
                await click_load_more(page, args.load_more_clicks, args.wait_ms)
                await cleanup_page_popups(page, args)
                await wait_for_page_ready(page, args, label="before-video-probe")
                probed_video_urls = await probe_video_playback(page, args)
                await cleanup_page_popups(page, args)
                await wait_for_page_ready(page, args, label="before-extract")
            except Exception as e:
                print(f"        [warn] scroll/load-more/video-probe failed: {e}")
                if is_browser_closed_error(e):
                    print("        [fatal] browser/tab/context closed during scroll/video probe. Stopping harvest.")
                    stop_requested = True
                    break

            try:
                dom = await extract_dom(page)
            except Exception as e:
                print(f"        [warn] DOM extraction failed: {e}")
                if is_browser_closed_error(e):
                    print("        [fatal] browser/tab/context closed during DOM extraction. Stopping harvest.")
                    stop_requested = True
                    break
                continue

            title = dom.get("title", "")
            html = dom.get("html", "")
            text = dom.get("text", "")
            dates = " | ".join(extract_dates(text + "\n" + html))
            current["title"] = title
            current["dates"] = dates

            raw_images = set(dom.get("images", []))
            raw_links = set(dom.get("links", []))
            regex_urls = extract_urls_by_regex(page_url, html + "\n" + text)
            raw_images.update([u for u in regex_urls if is_probably_image_url(u)])
            raw_links.update([u for u in regex_urls if is_probably_page_url(u)])

            image_urls = []
            video_urls = []
            for u in raw_images:
                full = safe_urljoin(page_url, u)
                if not full:
                    continue
                if is_probably_image_url(full):
                    if args.same_domain_images_only and not same_site(full, root_netloc, args.include_subdomains):
                        continue
                    image_urls.append(full)
                if args.harvest_videos and is_probably_video_url(full):
                    if args.same_domain_videos_only and not same_site(full, root_netloc, args.include_subdomains):
                        continue
                    video_urls.append(full)

            if args.harvest_videos and probed_video_urls:
                for full in probed_video_urls:
                    if args.same_domain_videos_only and not same_site(full, root_netloc, args.include_subdomains):
                        continue
                    if full not in video_urls:
                        video_urls.append(full)

            observed_video_urls = network_video_urls_by_page.get(page_url, set())
            if args.harvest_videos and observed_video_urls:
                for full in observed_video_urls:
                    if args.same_domain_videos_only and not same_site(full, root_netloc, args.include_subdomains):
                        continue
                    if full not in video_urls:
                        video_urls.append(full)
                print(f"        [network-media] observed {len(observed_video_urls)} media URLs from browser network")

            saved_before = len(store.records)
            videos_saved_before = len(video_store.records)
            if not args.network_only:
                await save_dom_image_urls(
                    context, store, image_urls, page_url, title, dates,
                    args.image_timeout_ms, concurrency=args.image_concurrency,
                    page=page,
                    args=args
                )
                if args.harvest_videos:
                    await save_dom_video_urls(
                        context, video_store, video_urls, page_url, title, dates,
                        args.video_timeout_ms, concurrency=args.video_concurrency,
                        max_video_mb=args.max_video_mb,
                        args=args
                    )
                    if args.concat_detected_ts_segments:
                        await download_ts_url_sequence(
                            context, video_store, video_urls, page_url, title, dates, args
                        )

            page_links = []
            for link in raw_links:
                full = safe_urljoin(page_url, link)
                if not full:
                    continue
                if not passes_url_filters(full, include_terms, exclude_terms):
                    continue
                if same_site(full, root_netloc, args.include_subdomains) and is_probably_page_url(full):
                    page_links.append(full)
                    if crawl_mode == "link" and full not in seen_pages and full not in queued:
                        queue.append(full)
                        queued.add(full)

            page_id = f"{len(seen_pages):05d}_{safe_filename(urlparse(page_url).path.replace('/', '_') or 'home', 90)}"
            html_path = html_dir / f"{page_id}.html"
            text_path = text_dir / f"{page_id}.txt"
            shot_path = shots_dir / f"{page_id}.png"

            if not args.no_html:
                html_path.write_text(html, encoding="utf-8", errors="replace")
                text_path.write_text(text, encoding="utf-8", errors="replace")
            else:
                html_path = Path("")
                text_path = Path("")

            if not args.no_screenshots:
                try:
                    await page.screenshot(path=str(shot_path), full_page=True)
                except Exception:
                    shot_path = Path("")
            else:
                shot_path = Path("")

            saved_on_page = len(store.records) - saved_before
            videos_saved_on_page = len(video_store.records) - videos_saved_before
            pages.append(PageRecord(
                page_url=page_url,
                title=title,
                extracted_dates=dates,
                image_urls_seen=len(image_urls),
                image_files_saved_on_page=saved_on_page,
                page_links_seen=len(page_links),
                saved_html=str(html_path),
                saved_text=str(text_path),
                saved_screenshot=str(shot_path) if str(shot_path) else "",
                blocked_or_verification=False,
            ))

            print(
                f"        image urls seen={len(image_urls)}, saved images={saved_on_page}, "
                f"video urls seen={len(video_urls)}, saved videos={videos_saved_on_page}, "
                f"total images={len(store.records)}, total videos={len(video_store.records)}, queue={len(queue)}"
            )

            store.flush_csv()
            video_store.flush_csv()
            write_pages_csv(pages, out / "pages_metadata.csv")

            if terminal_q_pressed():
                print("[stop] q pressed. Saving progress and stopping.")
                stop_requested = True
                break

            if args.delay_ms:
                await page.wait_for_timeout(args.delay_ms)

        store.flush_csv()
        video_store.flush_csv()
        write_pages_csv(pages, out / "pages_metadata.csv")

        manifest = {
            "finished_utc": now_iso(),
            "mode": "crawl",
            "start_url": root_url,
            "pages_seen": len(seen_pages),
            "image_records": len(store.records),
            "unique_image_files": len(store.seen_sha),
            "video_records": len(video_store.records),
            "unique_video_files": len(video_store.seen_sha),
            "harvest_videos": bool(args.harvest_videos),
            "stopped_by_user": stop_requested,
            "numbered_pages_used": bool(numbered_pages),
            "notes": [
                "This tool does not bypass Cloudflare or access controls.",
                "Images/videos are saved only when the browser/session can lawfully access them.",
                "images_metadata.csv links each image to source_url, final_url, found_on_page, dimensions, status, content type, SHA-256, and capture method.",
                "videos_metadata.csv links each video/media file or manifest to its source page and URL.",
                "HLS/DASH manifests may be saved, but segmented streams are not stitched or DRM-bypassed.",
            ],
        }
        (out / "harvest_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        if args.keep_open:
            print("\n[keep-open] Browser kept open. Press Enter to close/disconnect.")
            input("> ")

        if should_close_context and context is not None:
            await context.close()
        elif browser is not None and not args.connect_cdp:
            await browser.close()

    print("\nSaved:")
    print(f"  {store.metadata_csv}")
    print(f"  {store.metadata_jsonl}")
    if args.harvest_videos:
        print(f"  {video_store.metadata_csv}")
        print(f"  {video_store.metadata_jsonl}")
        print(f"  {out / 'videos'}")
    print(f"  {out / 'pages_metadata.csv'}")
    print(f"  {out / 'images'}")
    print(f"  {out / 'harvest_manifest.json'}")


def ingest_har(args: argparse.Namespace) -> None:
    out = mkdir(Path(args.output))
    store = ImageStore(out)
    pages: list[PageRecord] = []

    path = Path(args.har_file)
    if not path.exists():
        raise SystemExit(f"HAR not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    entries = data.get("log", {}).get("entries", [])

    for entry in entries:
        req = entry.get("request", {}) or {}
        res = entry.get("response", {}) or {}
        url = clean_url(req.get("url", ""))
        status = str(res.get("status", ""))
        content = res.get("content", {}) or {}
        mime = (content.get("mimeType") or "").lower()

        if "image/" in mime or is_probably_image_url(url):
            text = content.get("text", "")
            if text and content.get("encoding") == "base64":
                try:
                    body = base64.b64decode(text)
                    store.save(
                        data=body,
                        source_url=url,
                        final_url=url,
                        found_on_page=url,
                        page_title="",
                        page_dates="",
                        content_type=mime,
                        status=status,
                        capture_method="har_embedded_body",
                    )
                except Exception:
                    pass

        if "text/html" in mime:
            text = content.get("text", "")
            if text and content.get("encoding") == "base64":
                try:
                    text = base64.b64decode(text).decode("utf-8", errors="replace")
                except Exception:
                    text = ""
            dates = " | ".join(extract_dates(html_text_rough(text)))
            imgs = [u for u in extract_urls_by_regex(url, text) if is_probably_image_url(u)]
            pages.append(PageRecord(url, "", dates, len(imgs), 0, 0, "", "", "", False))

    store.flush_csv()
    write_pages_csv(pages, out / "pages_metadata.csv")
    (out / "harvest_manifest.json").write_text(json.dumps({
        "finished_utc": now_iso(),
        "mode": "ingest-har",
        "har_file": str(path),
        "image_records": len(store.records),
        "unique_image_files": len(store.seen_sha),
    }, indent=2), encoding="utf-8")

    print("\nSaved:")
    print(f"  {store.metadata_csv}")
    print(f"  {store.metadata_jsonl}")
    print(f"  {out / 'pages_metadata.csv'}")
    print(f"  {out / 'images'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract/download images from pages the browser can access and save metadata.")
    sub = parser.add_subparsers(dest="mode", required=True)

    crawl_p = sub.add_parser("crawl")
    crawl_p.add_argument("url")
    crawl_p.add_argument("--output", default="site_image_harvest")
    crawl_p.add_argument("--max-pages", type=int, default=1000)
    crawl_p.add_argument("--include-subdomains", action="store_true")
    crawl_p.add_argument("--same-domain-images-only", action="store_true")
    crawl_p.add_argument("--block-ads", action="store_true", help="Block common ad/tracker requests while crawling.")
    crawl_p.add_argument("--close-popups", action="store_true", help="Close popup tabs and remove/click common popup/ad overlay elements.")
    crawl_p.add_argument("--harvest-videos", action="store_true", help="Also harvest direct video/media files and HLS/DASH manifests when accessible.")
    crawl_p.add_argument("--same-domain-videos-only", action="store_true", help="Only download video/media URLs from the same domain.")
    crawl_p.add_argument("--probe-video-playback", action="store_true", help="Play/probe accessible <video> elements briefly to reveal currentSrc/network media URLs.")
    crawl_p.add_argument("--video-probe-seconds", type=float, default=4.0, help="Seconds to wait while probing/playing videos for media URLs.")
    crawl_p.add_argument("--max-video-elements", type=int, default=8, help="Maximum number of video elements to probe per page.")
    crawl_p.add_argument("--crawl-mode", choices=["auto", "numbered", "sitemap", "link", "single"], default="auto", help="How to discover pages: auto, numbered /page/{n}/, sitemap URLs, same-site link crawl, or single page only.")
    crawl_p.add_argument("--url-include", default="", help="Comma-separated URL terms to include, e.g. /gallery/,/post/. Empty allows all.")
    crawl_p.add_argument("--url-exclude", default="/tag/,/author/,/search/,/category/", help="Comma-separated URL terms to exclude while crawling.")
    crawl_p.add_argument("--headful", action="store_true")
    crawl_p.add_argument("--pause-first-page", action="store_true")
    crawl_p.add_argument("--stop-on-block", action="store_true")
    crawl_p.add_argument("--keep-open", action="store_true")
    crawl_p.add_argument("--connect-cdp", default="")
    crawl_p.add_argument("--user-data-dir", default="browser_profile_image_harvester")
    crawl_p.add_argument("--channel", default="", help="Optional Playwright channel, e.g. chrome")
    crawl_p.add_argument("--strict-https", action="store_true", help="Do not ignore HTTPS certificate errors. Default is to ignore certificate errors so bad certs do not stop evidence collection.")
    crawl_p.add_argument("--verify-url", default="", help="Optional URL to open first for manual verification before numbered harvesting starts.")
    crawl_p.add_argument("--wait-ms", type=int, default=6500)
    crawl_p.add_argument("--delay-ms", type=int, default=300)
    crawl_p.add_argument("--goto-timeout-ms", type=int, default=90000)
    crawl_p.add_argument("--image-timeout-ms", type=int, default=25000)
    crawl_p.add_argument("--viewport-width", type=int, default=1365)
    crawl_p.add_argument("--viewport-height", type=int, default=900)
    crawl_p.add_argument("--scroll-steps", type=int, default=6)
    crawl_p.add_argument("--scroll-delay-ms", type=int, default=350)
    crawl_p.add_argument("--scroll-until-bottom", action="store_true", help="Scroll to the bottom until page height stabilizes before extracting media.")
    crawl_p.add_argument("--max-bottom-scroll-rounds", type=int, default=30, help="Maximum bottom-scroll rounds for lazy loading.")
    crawl_p.add_argument("--bottom-scroll-delay-ms", type=int, default=800, help="Delay between bottom-scroll rounds.")
    crawl_p.add_argument("--bottom-stable-rounds", type=int, default=2, help="Stop when bottom/page height is stable for this many rounds.")
    crawl_p.add_argument("--lazy-final-wait-ms", type=int, default=2500, help="Final wait after lazy-load scrolling.")
    crawl_p.add_argument("--return-to-top-after-scroll", action="store_true", help="Return to top after lazy-load scrolling.")
    crawl_p.add_argument("--wait-load-state", choices=["", "load", "domcontentloaded", "networkidle"], default="", help="Optional Playwright load state to wait for before extraction. networkidle can hang on streaming sites.")
    crawl_p.add_argument("--load-state-timeout-ms", type=int, default=30000)
    crawl_p.add_argument("--extra-settle-ms", type=int, default=0, help="Extra wait before extraction/probing.")
    crawl_p.add_argument("--force-lazy-media", action="store_true", help="Promote common data-src/data-original lazy media attributes into src/srcset before extraction.")
    crawl_p.add_argument("--lazy-force-wait-ms", type=int, default=1500, help="Wait after forcing lazy media attributes.")
    crawl_p.add_argument("--page-fetch-images", action="store_true", help="Fallback: fetch image URLs inside the page with credentials included.")
    crawl_p.add_argument("--screenshot-image-fallback", action="store_true", help="Last resort: screenshot rendered image URLs when original bytes cannot be fetched.")
    crawl_p.add_argument("--load-more-clicks", type=int, default=0)
    crawl_p.add_argument("--discover-sitemaps", action="store_true", help="Legacy switch: in auto mode, this makes auto prefer sitemap crawling.")
    crawl_p.add_argument("--max-sitemap-urls", type=int, default=20000)
    crawl_p.add_argument("--max-image-mb", type=int, default=40)
    crawl_p.add_argument("--max-video-mb", type=int, default=100, help="Maximum size for a single video/media download.")
    crawl_p.add_argument("--video-timeout-ms", type=int, default=45000, help="Timeout for direct video/media URL downloads.")
    crawl_p.add_argument("--video-concurrency", type=int, default=2, help="Parallel direct video/media downloads per page. Keep low for stability.")
    crawl_p.add_argument("--download-hls-segments", action="store_true", help="Download and concatenate accessible non-encrypted HLS .ts segments from .m3u8 playlists.")
    crawl_p.add_argument("--concat-detected-ts-segments", action="store_true", help="Best-effort fallback: concatenate observed .ts URLs when no clean .m3u8 is found.")
    crawl_p.add_argument("--hls-max-segments", type=int, default=0, help="Limit HLS/TS segments per playlist/page. 0 means no segment-count limit; max_video_mb still applies.")
    crawl_p.add_argument("--hls-concurrency", type=int, default=4, help="Parallel HLS segment downloads.")
    crawl_p.add_argument("--auto-refresh-errors", action="store_true", help="Refresh temporary 502/503/504 Cloudflare/origin error pages until they recover or retries run out.")
    crawl_p.add_argument("--refresh-retries", type=int, default=5, help="How many times to refresh temporary error pages.")
    crawl_p.add_argument("--refresh-delay-ms", type=int, default=10000, help="Delay between temporary-error refresh attempts.")
    crawl_p.add_argument("--image-concurrency", type=int, default=8, help="Parallel DOM image downloads per page. Higher is faster but can trigger rate limits.")
    crawl_p.add_argument("--network-only", action="store_true", help="Fastest mode: only save images actually loaded by the browser/network; skip extra DOM URL downloads.")
    crawl_p.add_argument("--no-screenshots", action="store_true", help="Faster: do not save page screenshots.")
    crawl_p.add_argument("--no-html", action="store_true", help="Faster: do not save page HTML/text.")
    crawl_p.add_argument("--page-start", type=int, default=0, help="For sites like /page/1/, start page number.")
    crawl_p.add_argument("--page-end", type=int, default=0, help="For sites like /page/373/, end page number.")
    crawl_p.add_argument("--page-template", default="", help="Optional template, e.g. https://site.com/page/{n}/")

    har_p = sub.add_parser("ingest-har")
    har_p.add_argument("har_file")
    har_p.add_argument("--output", default="site_image_harvest_from_har")

    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.mode == "crawl":
        asyncio.run(crawl(args))
    elif args.mode == "ingest-har":
        ingest_har(args)
    else:
        raise SystemExit(f"Unknown mode: {args.mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
