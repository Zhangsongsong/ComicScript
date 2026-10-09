import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from rounman.comic_scraper import (
    ComicCrawler,
    discover_chapters,
    discover_book_links,
    extract_image_urls,
    safe_name,
    verify_image,
)
from bs4 import BeautifulSoup


class FakeResponse:
    def __init__(self, payload: bytes, content_type: str):
        self.payload = payload
        self.headers = {"Content-Type": content_type}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"
        self.text = payload.decode() if content_type.startswith("text/") else ""

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=1024):
        yield self.payload


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


class ScraperParsingTests(unittest.TestCase):
    def test_discovers_books_and_pages_without_treating_chapters_as_books(self):
        html = """
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


if __name__ == "__main__":
    unittest.main()
