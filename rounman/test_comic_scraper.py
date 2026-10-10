import io
import json
import tempfile
import unittest
from pathlib import Path

from curl_cffi.const import CurlHttpVersion, CurlOpt, CurlSslVersion
from curl_cffi.requests.exceptions import ProxyError, RequestException
from PIL import Image

from rounman.comic_scraper import (
    ComicCrawler,
    chapter_url_index,
    discover_chapters,
    discover_book_links,
    existing_chapter_directory,
    extract_image_urls,
    safe_name,
    quarantine_foreign_chapter_directories,
    verify_image,
)
from bs4 import BeautifulSoup


class FakeResponse:
    def __init__(self, payload: bytes, content_type: str):
        self.payload = payload
        self.status_code = 200
        self.headers = {"Content-Type": content_type}
        self.encoding = "utf-8"
        self.charset_encoding = "utf-8"
        self.text = payload.decode() if content_type.startswith("text/") else ""

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=1024):
        yield self.payload

    def close(self):
        return None


class FakeSession:
    def __init__(self, images):
        self.images = images

    def mount(self, *_args, **_kwargs):
        return None

    @property
    def headers(self):
        return self

    def update(self, *_args, **_kwargs):
        return None

    def get(self, url, **_kwargs):
        return FakeResponse(self.images[url], "image/jpeg")


class MemoryCrawler(ComicCrawler):
    def __init__(self, output_dir, pages, images):
        super().__init__(output_dir, workers=2, timeout=5)
        self.pages = pages
        self.session = FakeSession(images)

    def fetch(self, url, referer=None):
        return self.pages[url]


class FailingCrawler(ComicCrawler):
    def fetch(self, url, referer=None):
        raise RequestException("Connection reset by peer")


class ScraperParsingTests(unittest.TestCase):
    def test_discovers_books_and_pages_without_treating_chapters_as_books(self):
        html = """
        <header class="site-header"><a href="/home">Home</a></header>
        <nav class="pagination"><a href="/books?continued=false&page=2">2</a></nav>
        <article class="book-card"><a href="/book/demo"><img src="/cover.jpg" alt="Demo"></a></article>
        <div class="chapter"><a href="/book/demo/chapter/1">第1话</a></div>
        """
        soup = BeautifulSoup(html, "html.parser")
        books = discover_book_links(soup, "https://rouman5.com/books?continued=false")
        self.assertEqual(books, ["https://rouman5.com/book/demo"])

    def test_discovers_chapters_in_numeric_order_and_images(self):
        book_url = "https://rouman5.com/book/demo"
        html = """
        <a href="/book/demo/chapter/2">第2话</a>
        <a href="/book/demo/chapter/1">第1话</a>
        <img data-src="/images/001.webp" src="/loading.gif">
        <img src="https://img.example.test/002.jpg">
        """
        soup = BeautifulSoup(html, "html.parser")
        chapters = discover_chapters(soup, book_url)
        self.assertEqual([title for _, title in chapters], ["第1话", "第2话"])
        images = extract_image_urls(soup, "https://rouman5.com/book/demo/chapter/1")
        self.assertEqual(
            images,
            [
                "https://rouman5.com/images/001.webp",
                "https://img.example.test/002.jpg",
            ],
        )

    def test_uses_chapter_catalog_titles_and_zero_based_urls(self):
        book_url = "https://rouman5.com/books/demo"
        html = """
        <a href="/books/demo/0">開始閱讀</a>
        <section aria-label="章節目錄">
          <div class="site-chapters">
            <a class="site-chapter-link" href="/books/demo/0">
              <span title="第1話-20歲的女學生">第1話-20歲的女學生</span><small>↗</small>
            </a>
            <a class="site-chapter-link" href="/books/demo/1">
              <span title="第2話-我需要男人的撫摸">第2話-我需要男人的撫摸</span><small>↗</small>
            </a>
            <a class="site-chapter-link" href="/books/demo/53">
              <span title="最終話">最終話</span><small>NEW</small>
            </a>
          </div>
        </section>
        <a href="/books/another">另一部漫畫</a>
        """
        chapters = discover_chapters(BeautifulSoup(html, "html.parser"), book_url)
        self.assertEqual(
            chapters,
            [
                ("https://rouman5.com/books/demo/0", "第1話-20歲的女學生"),
                ("https://rouman5.com/books/demo/1", "第2話-我需要男人的撫摸"),
                ("https://rouman5.com/books/demo/53", "最終話"),
            ],
        )
        self.assertEqual(chapter_url_index(chapters[0][0], book_url), 0)
        self.assertEqual(chapter_url_index(chapters[-1][0], book_url), 53)

    def test_renames_existing_chapter_directory_from_manifest(self):
        chapter_url = "https://rouman5.com/books/demo/1"
        with tempfile.TemporaryDirectory() as directory:
            book_dir = Path(directory)
            old_dir = book_dir / "第2话"
            old_dir.mkdir()
            (old_dir / "manifest.json").write_text(
                json.dumps({"chapter_url": chapter_url}), encoding="utf-8"
            )
            desired = book_dir / "第2話-我需要男人的撫摸"

            result = existing_chapter_directory(book_dir, chapter_url, desired)

            self.assertEqual(result, desired)
            self.assertTrue(desired.is_dir())
            self.assertFalse(old_dir.exists())

    def test_quarantines_chapter_directory_from_another_book(self):
        book_url = "https://rouman5.com/books/demo"
        with tempfile.TemporaryDirectory() as directory:
            book_dir = Path(directory)
            valid_dir = book_dir / "第1话"
            valid_dir.mkdir()
            (valid_dir / "manifest.json").write_text(
                json.dumps({"chapter_url": f"{book_url}/chapter/1"}),
                encoding="utf-8",
            )
            wrong_dir = book_dir / "第7话"
            wrong_dir.mkdir()
            (wrong_dir / "manifest.json").write_text(
                json.dumps({"chapter_url": "https://rouman5.com/books/another"}),
                encoding="utf-8",
            )

            quarantine_foreign_chapter_directories(book_dir, book_url)

            self.assertFalse(wrong_dir.exists())
            self.assertTrue((book_dir / "_旧错误目录/第7话/manifest.json").is_file())
            self.assertTrue((valid_dir / "manifest.json").is_file())

    def test_prefers_reader_image_paths_over_icons_and_ads(self):
        html = """
        <img src="/android-chrome-192x192.png">
        <img src="https://ads.example.test/banner.jpg">
        <script>
        chapterData={imagePaths:$R[25]=["/pages/001.jpg","/pages/002.jpg"]}
        </script>
        """
        soup = BeautifulSoup(html, "html.parser")
        images = extract_image_urls(soup, "https://rouman5.com/books/demo/1", html)
        self.assertEqual(
            images,
            [
                "https://rouman5.com/pages/001.jpg",
                "https://rouman5.com/pages/002.jpg",
            ],
        )

    def test_safe_name_and_image_verification(self):
        self.assertEqual(safe_name("a:/b?c"), "a__b_c")
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory) / "valid.jpg"
            invalid = Path(directory) / "invalid.jpg"
            with Image.new("RGB", (2, 2), "white") as image:
                image.save(valid, format="JPEG")
            invalid.write_bytes(b"not an image")
            self.assertTrue(verify_image(valid))
            self.assertFalse(verify_image(invalid))

    def test_progress_and_resume_repairs_missing_or_corrupt_images(self):
        def make_jpeg(color):
            output = io.BytesIO()
            Image.new("RGB", (4, 4), color).save(output, format="JPEG")
            return output.getvalue()

        base = "https://example.test"
        start_url = f"{base}/books?continued=false"
        pages = {
            start_url: '<a href="/book/demo">Demo</a>',
            f"{base}/book/demo": '<h1>Demo</h1><a href="/book/demo/chapter/1">第1话</a>',
            f"{base}/book/demo/chapter/1": '<img src="/images/1.jpg"><img src="/images/2.jpg">',
        }
        images = {
            f"{base}/images/1.jpg": make_jpeg("red"),
            f"{base}/images/2.jpg": make_jpeg("blue"),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            crawler = MemoryCrawler(root, pages, images)
            crawler.download(start_url)

            chapter = root / "rouman5/第1页/Demo/第1话"
            (chapter / "0001.jpg").unlink()
            (chapter / "0002.jpg").write_bytes(b"broken")
            crawler.download(start_url)

            self.assertTrue(verify_image(chapter / "0001.jpg"))
            self.assertTrue(verify_image(chapter / "0002.jpg"))
            progress = json.loads((root / "rouman5/progress.json").read_text(encoding="utf-8"))
            self.assertIsNone(progress["current"])
            self.assertEqual(progress["pending_pages"], [])
            self.assertEqual(progress["books"][f"{base}/book/demo"]["status"], "completed")

    def test_proxy_is_used_for_page_and_image_requests(self):
        crawler = ComicCrawler(Path("download"), proxy="http://127.0.0.1:7890")
        self.assertFalse(crawler.session.trust_env)
        self.assertEqual(crawler.session.proxies["http"], "http://127.0.0.1:7890")
        self.assertEqual(crawler.session.proxies["https"], "http://127.0.0.1:7890")
        self.assertEqual(crawler.session.http_version, CurlHttpVersion.V3)
        self.assertEqual(crawler.session.curl_options[CurlOpt.SSLVERSION], CurlSslVersion.TLSv1_3)

    def test_proxy_connection_error_explains_that_the_proxy_must_be_running(self):
        crawler = ComicCrawler(Path("download"), proxy="http://127.0.0.1:7890")
        error = ProxyError("Cannot connect to proxy: Connection refused")
        with self.assertLogs("comic_scraper", level="ERROR") as logs:
            crawler.log_request_error("列表页请求", "https://example.test", error)

        output = "\n".join(logs.output)
        self.assertIn("HTTP/Mixed Port", output)
        self.assertIn("不会创建代理服务", output)

    def test_failed_listing_page_is_retained_and_reported_as_incomplete(self):
        start_url = "https://example.test/books?continued=false"
        with tempfile.TemporaryDirectory() as directory:
            crawler = FailingCrawler(Path(directory), timeout=1)
            with self.assertLogs("comic_scraper", level="WARNING") as logs:
                crawler.download(start_url)

            progress = json.loads(
                (Path(directory) / "rouman5/progress.json").read_text(encoding="utf-8")
            )
            self.assertTrue(crawler.had_errors)
            self.assertEqual(progress["pages"][start_url]["status"], "error")
            self.assertEqual(progress["pending_pages"], [{"number": 1, "url": start_url}])
            output = "\n".join(logs.output)
            self.assertIn("--proxy", output)
            self.assertIn("本次未完成", output)

    def test_page_limit_keeps_discovered_pages_for_resume(self):
        base = "https://example.test"
        start_url = f"{base}/books?continued=false"
        second_url = f"{base}/books?continued=false&page=2"
        pages = {
            start_url: f'<a class="pagination" href="{second_url}">2</a>',
            second_url: "<p>done</p>",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            crawler = MemoryCrawler(root, pages, {})
            crawler.download(start_url, max_pages=1)

            progress = json.loads((root / "rouman5/progress.json").read_text(encoding="utf-8"))
            self.assertEqual(progress["pending_pages"], [{"number": 2, "url": second_url}])


if __name__ == "__main__":
    unittest.main()
