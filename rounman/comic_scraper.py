#!/usr/bin/env python3
"""Download completed comics from rouman5.com.

The crawler intentionally keeps the HTML parsing rules tolerant because comic
sites tend to change CSS class names more often than their link structure.
Downloaded chapters contain a manifest so an interrupted run can validate and
repair existing files instead of treating an existing directory as complete.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import mimetypes
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Sequence
from urllib.parse import parse_qs, urldefrag, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag
from curl_cffi.const import CurlHttpVersion, CurlOpt, CurlSslVersion
from curl_cffi.requests import Session
from curl_cffi.requests.exceptions import ProxyError, RequestException
from PIL import Image


DEFAULT_START_URL = "https://rouman5.com/books?continued=false"
DEFAULT_OUTPUT_DIR = Path("download")
SITE_NAME = "rouman5"
MANIFEST_NAME = "manifest.json"
PROGRESS_NAME = "progress.json"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_REQUEST_RETRIES = 4

LOGGER = logging.getLogger("comic_scraper")
_manifest_lock = Lock()


@dataclass(frozen=True)
class ImageTask:
    index: int
    url: str
    referer: str
    filename: str


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def safe_name(value: str, fallback: str = "未命名") -> str:
    """Make a readable folder/file component on Windows, macOS and Linux."""

    value = clean_text(value)
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value)
    value = value.rstrip(" .")
    return (value[:180] or fallback).strip()


def canonical_url(base_url: str, href: str | None) -> str | None:
    if not href:
        return None
    href = html_lib.unescape(href).strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return None
    if href.startswith("//"):
        href = f"{urlparse(base_url).scheme}:{href}"
    absolute = urljoin(base_url, href)
    absolute, _ = urldefrag(absolute)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunparse(parsed._replace(fragment=""))


def same_site(left: str, right: str) -> bool:
    left_host = urlparse(left).netloc.lower().split(":", 1)[0]
    right_host = urlparse(right).netloc.lower().split(":", 1)[0]
    return left_host == right_host or left_host.removeprefix("www.") == right_host.removeprefix("www.")


def path_without_query(url: str) -> str:
    return urlparse(url).path.rstrip("/").lower() or "/"


def query_page_number(url: str) -> int | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("page", "p", "pageno", "page_no", "pageNum", "page_num"):
        values = query.get(key)
        if values and values[0].isdigit():
            return int(values[0])
    match = re.search(r"/(?:page[-_ ]?)?(\d+)/?$", parsed.path, re.I)
    return int(match.group(1)) if match else None


def progress_page_number(url: str, start_url: str) -> int:
    """Use the site's zero-based page query, with the unpaged start URL as page 0."""

    number = query_page_number(url)
    if number is not None:
        return number
    if canonical_url(start_url, url) == canonical_url(start_url, start_url):
        return 0
    return 0


def extract_title(soup: BeautifulSoup, url: str, fallback: str = "未命名漫画") -> str:
    for selector in ("h1", "h2", "meta[property='og:title']", "title"):
        node = soup.select_one(selector)
        if not node:
            continue
        value = node.get("content") if node.name == "meta" else node.get_text(" ", strip=True)
        value = clean_text(value)
        if value:
            value = re.sub(r"\s*[-|｜]\s*(漫画|漫画网|Rouman5).*$", "", value, flags=re.I)
            return safe_name(value, fallback)
    return fallback


def nearest_context_text(anchor: Tag) -> str:
    parts: list[str] = []
    current: Tag | None = anchor
    for _ in range(4):
        if current is None:
            break
        text = clean_text(current.get_text(" ", strip=True))
        if text:
            parts.append(text)
        current = current.parent if isinstance(current.parent, Tag) else None
    return " ".join(parts)


def looks_like_listing_url(url: str) -> bool:
    path = path_without_query(url)
    return path in {"/books", "/book", "/comics", "/comic", "/search", "/"}


def looks_like_chapter_url(url: str, text: str = "") -> bool:
    path = path_without_query(url)
    haystack = f"{path} {text}".lower()
    return bool(
        re.search(r"第\s*[0-9一二三四五六七八九十百千万]+\s*[话話集回]", text)
        or re.search(r"(?:chapter|chap|episode|ep|read|reader|chapterid|chapter_id)", haystack)
        or re.search(r"/(?:chapter|chapters|read|episode|episodes|chapterview)(?:/|$)", path)
    )


def is_probable_book_link(anchor: Tag, page_url: str) -> bool:
    href = canonical_url(page_url, anchor.get("href"))
    if not href or not same_site(href, page_url) or looks_like_listing_url(href):
        return False
    text = clean_text(anchor.get_text(" ", strip=True))
    path = path_without_query(href)
    known_detail = bool(re.fullmatch(r"/(?:book|books|comic|comics|manhua|detail|title)/[^/]+", path))
    if looks_like_chapter_url(href, text) and not known_detail:
        return False
    if re.search(
        r"/(?:home|login|register|auth|account|category|categories|tag|tags|author|authors|search|"
        r"contact|about|api|app-download|games|my-book-shelf|read-history)(?:/|$)",
        path,
    ):
        return False
    if re.search(r"\.(?:css|js|json|xml|jpg|jpeg|png|gif|webp|svg|ico)$", path):
        return False

    classes = " ".join(anchor.get("class", []))
    nearby_parents = [parent for parent in anchor.parents if isinstance(parent, Tag)][:3]
    parent_classes = " ".join(
        cls
        for parent in nearby_parents
        for cls in parent.get("class", [])
    )
    context = f"{classes} {parent_classes}".lower()
    card_context = re.search(r"book|comic|manga|manhua|cover|catalog|item|card", context)
    has_title = bool(text) or bool(anchor.find("img")) or bool(anchor.get("title"))
    return bool(known_detail or (card_context and has_title))


def discover_book_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = canonical_url(page_url, anchor.get("href"))
        if href and href not in seen and is_probable_book_link(anchor, page_url):
            seen.add(href)
            result.append(href)
    return result


def discover_page_links(soup: BeautifulSoup, page_url: str) -> list[tuple[int | None, str]]:
    result: list[tuple[int | None, str]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = canonical_url(page_url, anchor.get("href"))
        if not href or href in seen or not same_site(href, page_url):
            continue
        text = clean_text(anchor.get_text(" ", strip=True))
        context = f"{text} {' '.join(anchor.get('class', []))} {nearest_context_text(anchor)}".lower()
        number = query_page_number(href)
        has_page_parameter = any(key in parse_qs(urlparse(href).query) for key in ("page", "p", "pageno", "page_no", "page_num"))
        is_numeric_link = bool(re.fullmatch(r"(?:第\s*)?\d+", text))
        is_pagination_context = bool(re.search(r"pagination|pager|page-item|pages|分页", context))
        if (
            number is not None
            and (has_page_parameter or is_numeric_link or is_pagination_context)
            and not looks_like_chapter_url(href, text)
        ):
            seen.add(href)
            result.append((number, href))
    return result


def chapter_number(text: str) -> int | None:
    match = re.search(r"第\s*(\d+)\s*[话話集回]", text)
    if match:
        return int(match.group(1))
    match = re.search(r"(?:chapter|chap|episode|ep)[^0-9]*(\d+)", text, re.I)
    return int(match.group(1)) if match else None


def chapter_url_index(chapter_url: str, book_url: str) -> int | None:
    book_path = path_without_query(book_url)
    chapter_path = path_without_query(chapter_url)
    match = re.fullmatch(rf"{re.escape(book_path)}/(\d+)", chapter_path)
    return int(match.group(1)) if match else None


def chapter_belongs_to_book(chapter_url: str, book_url: str) -> bool:
    if not same_site(chapter_url, book_url):
        return False
    book_path = path_without_query(book_url).rstrip("/")
    chapter_path = path_without_query(chapter_url)
    return chapter_path.startswith(f"{book_path}/")


def discover_chapters(soup: BeautifulSoup, book_url: str) -> list[tuple[str, str]]:
    catalog = soup.select_one('section[aria-label="章節目錄"]')
    chapter_anchors = catalog.select("a.site-chapter-link[href]") if catalog else []
    if chapter_anchors:
        found: list[tuple[int, str, str]] = []
        seen: set[str] = set()
        for anchor in chapter_anchors:
            href = canonical_url(book_url, anchor.get("href"))
            if not href or href in seen or not same_site(href, book_url):
                continue
            url_index = chapter_url_index(href, book_url)
            if url_index is None:
                continue
            title_node = anchor.select_one("[title]")
            title = clean_text(title_node.get("title") if title_node else anchor.get_text(" ", strip=True))
            if not title:
                title = f"第{url_index + 1}話"
            seen.add(href)
            found.append((url_index, href, title))
        found.sort(key=lambda item: item[0])
        return [(href, title) for _, href, title in found]

    found: list[tuple[str, str, int | None]] = []
    seen: set[str] = set()
    book_path = path_without_query(book_url)
    for anchor in soup.select("a[href]"):
        href = canonical_url(book_url, anchor.get("href"))
        if not href or href in seen or not same_site(href, book_url) or href == book_url:
            continue
        text = clean_text(anchor.get_text(" ", strip=True) or anchor.get("title"))
        path = path_without_query(href)
        if re.search(r"/(?:login|register|account|category|tag|author|search)(?:/|$)", path):
            continue
        likely_child = path.startswith(f"{book_path}/") and path != book_path
        if not looks_like_chapter_url(href, text) and not likely_child:
            continue
        if not text:
            text = f"第{len(found) + 1}话"
        seen.add(href)
        found.append((href, text, chapter_number(text)))

    if any(number is not None for _, _, number in found):
        found.sort(key=lambda item: (item[2] is None, item[2] if item[2] is not None else 0))
    return [(href, text) for href, text, _ in found]


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".jfif"}
IMAGE_ATTRS = ("data-src", "data-original", "data-lazy-src", "data-url", "data-image", "src")


def is_image_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return Path(path).suffix in IMAGE_EXTENSIONS or "/image" in path or "/img" in path or "image" in path


def is_placeholder_image(url: str) -> bool:
    name = Path(urlparse(url).path).name.lower()
    return name in {
        "android-chrome-192x192.png",
        "android-chrome-512x512.png",
        "apple-touch-icon.png",
        "favicon.ico",
        "loading.gif",
        "lazy.gif",
        "blank.gif",
        "blank.jpg",
        "blank.png",
        "transparent.gif",
        "spacer.gif",
        "1x1.gif",
        "1x1.png",
    } or any(word in name for word in ("placeholder", "loading", "transparent"))


def srcset_urls(value: str | None) -> list[str]:
    if not value:
        return []
    entries: list[tuple[int, str]] = []
    for position, entry in enumerate(value.split(",")):
        bits = entry.strip().split()
        if not bits:
            continue
        width_match = re.search(r"(\d+)(?:w|x)$", bits[-1]) if len(bits) > 1 else None
        entries.append((int(width_match.group(1)) if width_match else position, bits[0]))
    return [url for _, url in sorted(entries)]


def embedded_json_array(raw_html: str, key: str) -> list:
    decoded = html_lib.unescape(raw_html).replace("\\/", "/")
    pattern = re.compile(rf"\b{re.escape(key)}\s*:\s*(?:\$R\[\d+\]\s*=\s*)?")
    decoder = json.JSONDecoder()
    for match in pattern.finditer(decoded):
        try:
            value, _ = decoder.raw_decode(decoded[match.end() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            return value
    return []


def extract_image_urls(soup: BeautifulSoup, page_url: str, raw_html: str = "") -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    def add(candidate: str | None) -> None:
        url = canonical_url(page_url, candidate)
        if not url or url in seen or not is_image_url(url) or is_placeholder_image(url):
            return
        seen.add(url)
        result.append(url)

    for candidate in embedded_json_array(raw_html, "imagePaths"):
        if isinstance(candidate, str):
            add(candidate)
    if result:
        return result

    for image in soup.select("img"):
        candidates: list[str] = []
        for attr in IMAGE_ATTRS:
            candidates.append(image.get(attr, ""))
        candidates.extend(srcset_urls(image.get("srcset")))
        for candidate in candidates:
            if candidate and not candidate.startswith("data:"):
                add(candidate)

    for node in soup.select("source[srcset], video[poster]"):
        for candidate in srcset_urls(node.get("srcset")):
            add(candidate)
        add(node.get("poster"))

    # Some readers put their page list in an inline JSON object rather than img tags.
    decoded = html_lib.unescape(raw_html).replace("\\/", "/")
    for candidate in re.findall(
        r"(?:(?:https?:)?//[^\"'<>\\ ]+|/[^\"'<>\\ ]+?)\.(?:jpg|jpeg|png|gif|webp|bmp|avif)(?:\?[^\"'<>\\ ]*)?",
        decoded,
        flags=re.I,
    ):
        add(candidate)

    return result


def extension_from_url(url: str, content_type: str | None = None) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return ".jpg" if suffix == ".jpeg" else suffix
    guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip())
    return guessed if guessed in IMAGE_EXTENSIONS else ".jpg"


def verify_image(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 32:
            return False
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
        return True
    except (OSError, ValueError):
        return False


def load_manifest(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_manifest(path: Path, manifest: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with _manifest_lock:
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def existing_valid_image(directory: Path, stem: str) -> Path | None:
    for path in sorted(directory.glob(f"{stem}.*")):
        if verify_image(path):
            return path
    return None


def existing_chapter_directory(book_dir: Path, chapter_url: str, desired: Path) -> Path:
    if desired.exists():
        return desired
    for manifest_path in book_dir.glob(f"*/{MANIFEST_NAME}"):
        manifest = load_manifest(manifest_path)
        if manifest.get("chapter_url") != chapter_url:
            continue
        current = manifest_path.parent
        if current == desired:
            return desired
        try:
            current.rename(desired)
            LOGGER.info("章节目录已按网页标题重命名: %s -> %s", current.name, desired.name)
            return desired
        except OSError as error:
            LOGGER.warning("无法重命名章节目录 %s: %s", current, error)
            return current
    return desired


def quarantine_foreign_chapter_directories(book_dir: Path, book_url: str) -> None:
    quarantine_dir = book_dir / "_旧错误目录"
    for manifest_path in list(book_dir.glob(f"*/{MANIFEST_NAME}")):
        manifest = load_manifest(manifest_path)
        chapter_url = manifest.get("chapter_url")
        if not isinstance(chapter_url, str) or chapter_belongs_to_book(chapter_url, book_url):
            continue
        current = manifest_path.parent
        quarantine_dir.mkdir(exist_ok=True)
        target = quarantine_dir / current.name
        suffix = 2
        while target.exists():
            target = quarantine_dir / f"{current.name}_{suffix}"
            suffix += 1
        try:
            current.rename(target)
            LOGGER.warning("已隔离误抓的章节目录: %s -> %s", current.name, target)
        except OSError as error:
            LOGGER.warning("无法隔离误抓的章节目录 %s: %s", current, error)


class ComicCrawler:
    def __init__(
        self,
        output_dir: Path,
        workers: int = 4,
        timeout: float = 30.0,
        delay: float = 0.0,
        proxy: str | None = None,
    ) -> None:
        self.output_dir = output_dir
        self.workers = max(1, workers)
        self.timeout = timeout
        self.delay = max(0.0, delay)
        self.proxy = proxy
        proxies = {"http": proxy, "https": proxy} if proxy else None
        self.session = Session(
            http_version=CurlHttpVersion.V3,
            curl_options={CurlOpt.SSLVERSION: CurlSslVersion.TLSv1_3},
            trust_env=not bool(proxy),
            proxies=proxies,
        )
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        self.progress_path: Path | None = None
        self.progress: dict = {}
        self.had_errors = False

    def log_request_error(self, context: str, url: str, error: RequestException) -> None:
        LOGGER.error("%s失败，已保留进度: %s: %s", context, url, error)
        message = str(error).lower()
        if isinstance(error, ProxyError) or "cannot connect to proxy" in message:
            LOGGER.error(
                "无法连接指定代理。请先启动代理软件，并将 --proxy 后的端口改成软件显示的 HTTP/Mixed Port；"
                "仅填写 127.0.0.1:7890 不会创建代理服务。"
            )
            return
        if "connection reset by peer" not in message and "connectionreseterror" not in message:
            return
        if self.proxy:
            LOGGER.error("连接经过代理后仍被重置，请确认代理可访问该站点并支持 HTTPS CONNECT。")
        else:
            LOGGER.error(
                "HTTP/3 连接仍在收到响应前被重置。若浏览器可以访问，请检查浏览器是否使用了代理扩展；"
                "也可以启用相同代理后使用 --proxy http://127.0.0.1:端口 重试。"
            )

    def save_progress(self) -> None:
        if self.progress_path is None:
            return
        self.progress["updated_at"] = utc_timestamp()
        save_manifest(self.progress_path, self.progress)

    @staticmethod
    def pending_snapshot(pending: list[tuple[int, str]]) -> list[dict]:
        return [{"number": number, "url": url} for number, url in pending]

    def migrate_zero_page_layout(self, start_url: str, base_dir: Path) -> None:
        """Move legacy unpaged downloads from 第1页 to 第0页 using progress metadata."""

        if self.progress.get("page_directory_layout") == "zero_based_query_parameter":
            return

        legacy_dir = base_dir / safe_name("第1页")
        zero_dir = base_dir / safe_name("第0页")
        moved = 0
        for book_state in self.progress.get("books", {}).values():
            if not isinstance(book_state, dict) or book_state.get("page_url") != start_url:
                continue
            book_state["page_number"] = 0
            title = book_state.get("title")
            if not isinstance(title, str) or not title:
                continue
            source = legacy_dir / safe_name(title)
            target = zero_dir / safe_name(title)
            if not source.exists():
                continue
            if target.exists():
                LOGGER.warning("第0页已存在同名目录，保留原目录不迁移: %s", source)
                continue
            zero_dir.mkdir(parents=True, exist_ok=True)
            source.rename(target)
            moved += 1

        page_state = self.progress.get("pages", {}).get(start_url)
        if isinstance(page_state, dict):
            page_state["number"] = 0
        current = self.progress.get("current")
        if isinstance(current, dict) and current.get("page_url"):
            current["page_number"] = progress_page_number(current["page_url"], start_url)
        for item in self.progress.get("pending_pages", []):
            if isinstance(item, dict) and item.get("url"):
                item["number"] = progress_page_number(item["url"], start_url)

        self.progress["page_directory_layout"] = "zero_based_query_parameter"
        if moved:
            LOGGER.info("旧目录迁移完成：已将 %s 部漫画从第1页移动到第0页", moved)

    def initialize_progress(self, start_url: str, base_dir: Path) -> list[tuple[int, str]]:
        self.progress_path = base_dir / PROGRESS_NAME
        previous = load_manifest(self.progress_path)
        if previous.get("start_url") == start_url:
            self.progress = previous
        else:
            self.progress = {
                "site": SITE_NAME,
                "start_url": start_url,
                "created_at": utc_timestamp(),
                "updated_at": utc_timestamp(),
                "current": None,
                "pending_pages": [],
                "pages": {},
                "books": {},
            }

        self.migrate_zero_page_layout(start_url, base_dir)

        pending: list[tuple[int, str]] = []
        current = self.progress.get("current")
        if isinstance(current, dict) and current.get("page_url"):
            pending.append((progress_page_number(current["page_url"], start_url), current["page_url"]))
        for item in self.progress.get("pending_pages", []):
            if not isinstance(item, dict) or not item.get("url"):
                continue
            page_url = item["url"]
            if page_url != (current or {}).get("page_url"):
                page_state = self.progress.get("pages", {}).get(page_url, {})
                if page_state.get("status") == "completed":
                    continue
            pending.append((progress_page_number(page_url, start_url), page_url))
        if not pending:
            pending.append((0, start_url))

        deduplicated: list[tuple[int, str]] = []
        seen: set[str] = set()
        for number, url in pending:
            if url not in seen:
                seen.add(url)
                deduplicated.append((number, url))
        self.progress["pending_pages"] = self.pending_snapshot(deduplicated)
        self.save_progress()
        if len(deduplicated) > 1 or deduplicated[0][1] != start_url:
            current_page = (self.progress.get("current") or {}).get("page_url", deduplicated[0][1])
            LOGGER.info("发现上次下载进度，将从 %s 继续", current_page)
        return deduplicated

    def request(self, url: str, referer: str | None = None, stream: bool = False):
        headers = {"Referer": referer} if referer else {}
        for attempt in range(MAX_REQUEST_RETRIES + 1):
            if self.delay:
                time.sleep(self.delay)
            try:
                response = self.session.get(url, headers=headers, timeout=self.timeout, stream=stream)
            except RequestException as error:
                if isinstance(error, ProxyError) or attempt >= MAX_REQUEST_RETRIES:
                    raise
                wait_seconds = 2 ** attempt
                LOGGER.warning(
                    "请求连接失败，%s 秒后重试（%s/%s）: %s",
                    wait_seconds,
                    attempt + 1,
                    MAX_REQUEST_RETRIES,
                    error,
                )
                time.sleep(wait_seconds)
                continue

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_REQUEST_RETRIES:
                retry_after = response.headers.get("Retry-After", "")
                wait_seconds = int(retry_after) if str(retry_after).isdigit() else 2 ** attempt
                response.close()
                LOGGER.warning(
                    "网站返回 HTTP %s，%s 秒后重试（%s/%s）",
                    response.status_code,
                    wait_seconds,
                    attempt + 1,
                    MAX_REQUEST_RETRIES,
                )
                time.sleep(wait_seconds)
                continue

            response.raise_for_status()
            return response
        raise RuntimeError("请求重试逻辑异常结束")

    def fetch(self, url: str, referer: str | None = None) -> str:
        response = self.request(url, referer=referer)
        try:
            response.encoding = response.charset_encoding or response.encoding or "utf-8"
            return response.text
        finally:
            response.close()

    def download(self, start_url: str, max_pages: int | None = None, max_books: int | None = None) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        base_dir = self.output_dir / safe_name(SITE_NAME)
        base_dir.mkdir(parents=True, exist_ok=True)
        pending = self.initialize_progress(start_url, base_dir)
        resume_page_url = (self.progress.get("current") or {}).get("page_url")
        deferred: list[tuple[int, str]] = []
        seen_pages: set[str] = set()
        seen_books: set[str] = set()
        pages_processed = 0
        books_processed = 0

        def pending_pages() -> list[tuple[int, str]]:
            result: list[tuple[int, str]] = []
            seen: set[str] = set()
            for item in [*pending, *deferred]:
                if item[1] not in seen:
                    seen.add(item[1])
                    result.append(item)
            return result

        def save_pending_pages() -> None:
            self.progress["pending_pages"] = self.pending_snapshot(pending_pages())

        def defer_page(number: int, url: str) -> None:
            if url not in {item_url for _, item_url in deferred}:
                deferred.append((number, url))

        while pending:
            if max_pages is not None and pages_processed >= max_pages:
                break
            if resume_page_url:
                resume_index = next((index for index, item in enumerate(pending) if item[1] == resume_page_url), None)
                if resume_index is not None:
                    page_number, page_url = pending.pop(resume_index)
                else:
                    pending.sort(key=lambda item: (item[0], item[1]))
                    page_number, page_url = pending.pop(0)
                resume_page_url = None
            else:
                pending.sort(key=lambda item: (item[0], item[1]))
                page_number, page_url = pending.pop(0)
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)
            page_state = self.progress.setdefault("pages", {}).setdefault(page_url, {})
            page_state.update({"number": page_number, "status": "in_progress", "started_at": utc_timestamp()})
            self.progress["current"] = {"page_number": page_number, "page_url": page_url, "status": "in_progress"}
            save_pending_pages()
            self.save_progress()
            LOGGER.info("扫描第 %s 页: %s", page_number, page_url)
            try:
                page_html = self.fetch(page_url)
            except RequestException as error:
                self.log_request_error("列表页请求", page_url, error)
                self.had_errors = True
                page_state.update({"status": "error", "error": str(error), "updated_at": utc_timestamp()})
                defer_page(page_number, page_url)
                save_pending_pages()
                self.progress["current"] = None
                self.save_progress()
                continue

            soup = BeautifulSoup(page_html, "html.parser")
            page_links = discover_page_links(soup, page_url)
            for discovered_number, discovered_url in page_links:
                if discovered_url in seen_pages or discovered_url in {url for _, url in pending_pages()}:
                    continue
                discovered_state = self.progress.get("pages", {}).get(discovered_url, {})
                if discovered_state.get("status") == "completed":
                    continue
                number = discovered_number if discovered_number is not None else progress_page_number(discovered_url, start_url)
                pending.append((number, discovered_url))
            save_pending_pages()
            self.save_progress()

            page_dir = base_dir / safe_name(f"第{page_number}页")
            book_links = discover_book_links(soup, page_url)
            LOGGER.info("第 %s 页发现 %s 部漫画", page_number, len(book_links))
            page_had_errors = False
            for book_url in book_links:
                if book_url in seen_books or (max_books is not None and books_processed >= max_books):
                    continue
                seen_books.add(book_url)
                books_processed += 1
                book_state = self.progress.setdefault("books", {}).setdefault(book_url, {})
                book_state.update({"page_number": page_number, "page_url": page_url, "status": "in_progress"})
                self.progress["current"] = {
                    "page_number": page_number,
                    "page_url": page_url,
                    "book_url": book_url,
                    "status": "in_progress",
                }
                self.save_progress()
                try:
                    completed = self.download_book(book_url, page_dir)
                    book_state.update({"status": "completed" if completed else "no_chapters", "updated_at": utc_timestamp()})
                    if completed:
                        book_state.pop("error", None)
                    page_had_errors = page_had_errors or not completed
                except RequestException as error:
                    self.log_request_error("漫画请求", book_url, error)
                    self.had_errors = True
                    book_state.update({"status": "error", "error": str(error), "updated_at": utc_timestamp()})
                    page_had_errors = True
                except Exception:
                    LOGGER.exception("处理漫画时发生未预期错误: %s", book_url)
                    self.had_errors = True
                    book_state.update({"status": "error", "error": "unexpected error", "updated_at": utc_timestamp()})
                    page_had_errors = True
                self.save_progress()
            limit_reached = max_books is not None and books_processed >= max_books
            page_needs_retry = page_had_errors or limit_reached
            if page_needs_retry:
                defer_page(page_number, page_url)
            page_state.update(
                {
                    "status": "limited" if limit_reached else ("incomplete" if page_had_errors else "completed"),
                    "book_count": len(book_links),
                    "updated_at": utc_timestamp(),
                }
            )
            if not page_needs_retry:
                page_state.pop("error", None)
            self.progress["current"] = None
            save_pending_pages()
            self.save_progress()
            pages_processed += 1
            if limit_reached:
                break

        save_pending_pages()
        self.progress["current"] = None
        self.save_progress()
        remaining_pages = len(self.progress.get("pending_pages", []))
        if self.had_errors or remaining_pages:
            LOGGER.warning(
                "本次未完成：成功扫描 %s 页，处理 %s 部漫画，仍有 %s 个列表页待处理；再次运行会从断点继续",
                pages_processed,
                books_processed,
                remaining_pages,
            )
        else:
            LOGGER.info("完成：扫描 %s 页，处理 %s 部漫画", pages_processed, books_processed)

    def download_book(self, book_url: str, page_dir: Path) -> bool:
        LOGGER.info("读取漫画: %s", book_url)
        book_html = self.fetch(book_url, referer=book_url)
        soup = BeautifulSoup(book_html, "html.parser")
        title = extract_title(soup, book_url)
        chapters = discover_chapters(soup, book_url)
        book_state = self.progress.setdefault("books", {}).setdefault(book_url, {})
        book_state.update({"title": title, "chapters_total": len(chapters), "updated_at": utc_timestamp()})
        self.progress["current"] = {**(self.progress.get("current") or {}), "book_url": book_url, "title": title}
        self.save_progress()
        if not chapters:
            LOGGER.warning("没有找到章节: %s", book_url)
            return False

        book_dir = page_dir / safe_name(title)
        book_dir.mkdir(parents=True, exist_ok=True)
        LOGGER.info("漫画《%s》发现 %s 话", title, len(chapters))
        quarantine_foreign_chapter_directories(book_dir, book_url)
        chapter_states = book_state.setdefault("chapters", {})
        valid_chapter_urls = {chapter_url for chapter_url, _ in chapters}
        chapter_title_counts = Counter(chapter_title for _, chapter_title in chapters)
        for stale_url in list(chapter_states):
            if stale_url not in valid_chapter_urls:
                del chapter_states[stale_url]
        all_chapters_completed = True
        for index, (chapter_url, chapter_title) in enumerate(chapters, start=1):
            url_index = chapter_url_index(chapter_url, book_url)
            number = chapter_number(chapter_title) or ((url_index + 1) if url_index is not None else index)
            directory_title = chapter_title
            if chapter_title_counts[chapter_title] > 1:
                unique_index = url_index if url_index is not None else index
                directory_title = f"{chapter_title} [{unique_index}]"
            desired_dir = book_dir / safe_name(directory_title, f"第{number}話")
            chapter_dir = existing_chapter_directory(book_dir, chapter_url, desired_dir)
            chapter_state = chapter_states.setdefault(chapter_url, {})
            chapter_state.update({"number": number, "title": chapter_title, "status": "in_progress"})
            self.progress["current"] = {
                **(self.progress.get("current") or {}),
                "chapter_url": chapter_url,
                "chapter_title": chapter_title,
                "chapter_number": number,
            }
            self.save_progress()
            try:
                chapter_completed = self.download_chapter(
                    chapter_url, chapter_dir, book_url, number, chapter_title, chapter_state
                )
                chapter_state.update(
                    {"status": "completed" if chapter_completed else "no_images", "updated_at": utc_timestamp()}
                )
                if chapter_completed:
                    chapter_state.pop("error", None)
                all_chapters_completed = all_chapters_completed and chapter_completed
            except Exception as error:
                chapter_state.update({"status": "error", "error": str(error), "updated_at": utc_timestamp()})
                self.save_progress()
                raise
            self.save_progress()
        return all_chapters_completed

    def download_chapter(
        self,
        chapter_url: str,
        chapter_dir: Path,
        book_url: str,
        chapter_number_value: int,
        chapter_title: str,
        progress_state: dict | None = None,
    ) -> bool:
        LOGGER.info("读取 %s: %s", chapter_title, chapter_url)
        chapter_html = self.fetch(chapter_url, referer=book_url)
        soup = BeautifulSoup(chapter_html, "html.parser")
        image_urls = extract_image_urls(soup, chapter_url, chapter_html)
        if not image_urls:
            LOGGER.warning("没有找到图片: %s", chapter_url)
            if progress_state is not None:
                progress_state.update({"status": "no_images", "images_total": 0, "images_completed": 0})
                self.save_progress()
            return False

        chapter_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = chapter_dir / MANIFEST_NAME
        old_manifest = load_manifest(manifest_path)
        old_images = {
            item.get("index"): item
            for item in old_manifest.get("images", [])
            if isinstance(item, dict) and isinstance(item.get("index"), int)
        }
        manifest = {
            "chapter_url": chapter_url,
            "chapter_title": chapter_title,
            "chapter_number": chapter_number_value,
            "images": [],
        }

        tasks: list[ImageTask] = []
        for index, image_url in enumerate(image_urls, start=1):
            previous = old_images.get(index, {})
            previous_filename = previous.get("filename") if isinstance(previous, dict) else None
            extension = Path(previous_filename).suffix if previous_filename else extension_from_url(image_url)
            filename = f"{index:04d}{extension.lower() if extension else '.jpg'}"
            task = ImageTask(index=index, url=image_url, referer=chapter_url, filename=filename)
            tasks.append(task)
            manifest["images"].append({"index": index, "url": image_url, "filename": filename})

        save_manifest(manifest_path, manifest)
        if progress_state is not None:
            progress_state.update({"status": "downloading", "images_total": len(tasks), "images_completed": 0})
            self.save_progress()
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = [
                executor.submit(self.ensure_image, task, chapter_dir, old_images.get(task.index, {}))
                for task in tasks
            ]
            completed_count = 0
            try:
                for future in as_completed(futures):
                    future.result()
                    completed_count += 1
                    if progress_state is not None:
                        progress_state["images_completed"] = completed_count
                        self.save_progress()
            except Exception as error:
                if progress_state is not None:
                    progress_state.update({"status": "error", "error": str(error), "images_completed": completed_count})
                    self.save_progress()
                raise
        save_manifest(manifest_path, manifest)
        if progress_state is not None:
            progress_state.update({"status": "completed", "images_completed": len(tasks)})
            self.save_progress()
        LOGGER.info("完成 %s：%s 张图片", chapter_dir.name, len(tasks))
        return True

    def ensure_image(self, task: ImageTask, chapter_dir: Path, previous: dict) -> None:
        target = chapter_dir / task.filename
        previous_url = previous.get("url") if isinstance(previous, dict) else None
        if previous_url == task.url and verify_image(target):
            LOGGER.debug("跳过完整图片: %s", target)
            return

        # If an older run used a different extension, a valid numbered image is
        # still useful when the source URL did not change.
        if previous_url == task.url:
            existing = existing_valid_image(chapter_dir, f"{task.index:04d}")
            if existing and existing != target:
                LOGGER.debug("跳过完整图片: %s", existing)
                return

        temporary = target.with_name(f".{target.name}.part")
        response = None
        try:
            response = self.request(task.url, referer=task.referer, stream=True)
            with temporary.open("wb") as stream:
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    if chunk:
                        stream.write(chunk)
            if not verify_image(temporary):
                raise OSError("下载内容不是有效图片")
            temporary.replace(target)
            LOGGER.debug("下载图片 %s", target)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        finally:
            if response is not None:
                response.close()


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="下载 rouman5.com 已完结漫画")
    parser.add_argument("--url", default=DEFAULT_START_URL, help="已完结漫画列表页")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR, help="下载根目录，默认 download")
    parser.add_argument("--workers", type=int, default=4, help="同时下载图片的数量，默认 4")
    parser.add_argument("--timeout", type=float, default=30, help="单次请求超时秒数，默认 30")
    parser.add_argument("--delay", type=float, default=0, help="请求之间的间隔秒数，默认 0")
    parser.add_argument("--proxy", help="HTTP/HTTPS 代理，例如 http://127.0.0.1:7890")
    parser.add_argument("--max-pages", type=int, default=None, help="最多扫描多少个列表页，调试时使用")
    parser.add_argument("--max-books", type=int, default=None, help="最多下载多少部漫画，调试时使用")
    parser.add_argument("--verbose", action="store_true", help="输出更详细的下载日志")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        crawler = ComicCrawler(
            output_dir=args.output,
            workers=args.workers,
            timeout=args.timeout,
            delay=args.delay,
            proxy=args.proxy,
        )
        crawler.download(args.url, max_pages=args.max_pages, max_books=args.max_books)
        return 1 if crawler.had_errors else 0
    except KeyboardInterrupt:
        LOGGER.warning("用户中断。再次运行会校验并补齐已有章节。")
        return 130
    except RequestException as error:
        LOGGER.error("请求失败: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
