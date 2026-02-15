import shutil
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

import requests

from reader.models import Block, Book, Bookmark, CustomUser, ReadingProgress, Section, TranslationCache
from reader.utils import _content_hash, sanitize_book_translations, translate_html_with_ollama


class ReaderSecurityTests(TestCase):
    def setUp(self):
        self.user_a = CustomUser.objects.create_user(email="a@example.com", password="pass1234")
        self.user_b = CustomUser.objects.create_user(email="b@example.com", password="pass1234")
        self.media_root = tempfile.mkdtemp(prefix="epubdrop-tests-")
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)

    def _make_ready_book(self, owner, title="Libro", status=Book.Status.READY):
        book = Book.objects.create(
            owner=owner,
            title=title,
            status=status,
            total_blocks=1,
            translated_blocks=1 if status == Book.Status.READY else 0,
        )
        section = Section.objects.create(book=book, index=0)
        Block.objects.create(
            section=section,
            index=0,
            original_html="<p>Original</p>",
            translated_html="<p>Traducido</p>",
        )
        return book

    def test_upload_requires_login(self):
        response = self.client.get(reverse("upload_epub"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_translation_progress_returns_only_own_books(self):
        own_book = Book.objects.create(
            owner=self.user_a,
            title="Own in progress",
            status=Book.Status.TRANSLATING,
            total_blocks=10,
            translated_blocks=2,
        )
        Book.objects.create(
            owner=self.user_b,
            title="Other in progress",
            status=Book.Status.TRANSLATING,
            total_blocks=10,
            translated_blocks=7,
        )

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("translation_progress"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()["in_progress"]
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["id"], str(own_book.id))

    def test_read_book_returns_404_for_non_owner(self):
        other_book = self._make_ready_book(owner=self.user_b)
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("read_book", kwargs={"book_id": str(other_book.id)}))
        self.assertEqual(response.status_code, 404)

    def test_save_progress_returns_404_for_non_owner(self):
        other_book = self._make_ready_book(owner=self.user_b)
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("save_progress", kwargs={"book_id": str(other_book.id)}),
            data={
                "section_idx": 0,
                "block_idx": 0,
                "progress_percent": 10,
                "block_offset_percent": 0.2,
                "block_id": 0,
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "missing_book")

    def test_save_progress_persists_for_owner_and_is_exposed_in_read_view(self):
        own_book = self._make_ready_book(owner=self.user_a)
        block = Block.objects.filter(section__book=own_book).first()

        self.client.force_login(self.user_a)
        save_response = self.client.post(
            reverse("save_progress", kwargs={"book_id": str(own_book.id)}),
            data={
                "section_idx": 0,
                "block_idx": 0,
                "progress_percent": 42,
                "block_offset_percent": 0.35,
                "block_id": block.id,
            },
        )
        self.assertEqual(save_response.status_code, 200)

        rp = ReadingProgress.objects.get(book=own_book, user=self.user_a)
        self.assertEqual(rp.block_id, block.id)
        self.assertEqual(rp.section_index, 0)
        self.assertEqual(rp.block_index, 0)
        self.assertEqual(rp.progress_percent, 42)
        self.assertAlmostEqual(rp.block_offset_percent, 0.35, places=3)

        read_response = self.client.get(reverse("read_book", kwargs={"book_id": str(own_book.id)}))
        self.assertEqual(read_response.status_code, 200)
        self.assertContains(read_response, f'data-saved-block-id="{block.id}"')

    def test_translate_block_for_non_owner_returns_missing_book_fallback(self):
        other_book = self._make_ready_book(owner=self.user_b)
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse(
                "translate_block",
                kwargs={"book_id": str(other_book.id), "section_idx": 0, "block_idx": 0},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["fallback"], "missing_book")

    def test_clear_book_allows_owner_only(self):
        own_book = Book.objects.create(
            owner=self.user_a,
            title="Libro",
            status=Book.Status.TRANSLATING,
            total_blocks=1,
            translated_blocks=0,
        )
        other_book = Book.objects.create(
            owner=self.user_b,
            title="Libro",
            status=Book.Status.TRANSLATING,
            total_blocks=1,
            translated_blocks=0,
        )

        own_book_dir = Path(self.media_root) / "epub_books" / str(own_book.id)
        own_assets_dir = Path(self.media_root) / "epub_assets" / str(own_book.id)
        own_book_dir.mkdir(parents=True, exist_ok=True)
        own_assets_dir.mkdir(parents=True, exist_ok=True)
        (own_book_dir / "book.epub").write_bytes(b"PK\x03\x04dummy")
        (own_assets_dir / "asset.txt").write_text("x", encoding="utf-8")

        with self.settings(MEDIA_ROOT=self.media_root):
            self.client.force_login(self.user_a)
            response = self.client.post(reverse("clear_book", kwargs={"book_id": str(own_book.id)}))
            self.assertEqual(response.status_code, 302)
            self.assertFalse(Book.objects.filter(pk=own_book.id).exists())
            self.assertFalse(own_book_dir.exists())
            self.assertFalse(own_assets_dir.exists())

            response = self.client.post(reverse("clear_book", kwargs={"book_id": str(other_book.id)}))
            self.assertEqual(response.status_code, 404)
            self.assertTrue(Book.objects.filter(pk=other_book.id).exists())

    def test_upload_sets_owner(self):
        epub_file = SimpleUploadedFile(
            "test.epub",
            b"PK\x03\x04fake-epub",
            content_type="application/epub+zip",
        )
        with self.settings(MEDIA_ROOT=self.media_root):
            self.client.force_login(self.user_a)
            with patch("reader.views.validate_epub_file", return_value=(True, None)), patch(
                "reader.views.extract_epub_info_from_path",
                return_value={"title": "Mock Book", "authors": ["Author"], "description_html": "", "cover_url": ""},
            ), patch(
                "reader.views.build_reader_sections_with_blocks_from_spine",
                return_value=[{"id": "s0", "blocks": ["<p>uno</p>"]}],
            ), patch("reader.views.start_book_translation_async", return_value=None):
                response = self.client.post(
                    reverse("upload_epub"),
                    data={"epub_file": epub_file, "notify_email": "owner@example.com"},
                )

        self.assertEqual(response.status_code, 302)
        created = Book.objects.get(title="Mock Book")
        self.assertEqual(created.owner_id, self.user_a.id)


class TranslationQualityTests(TestCase):
    def test_translate_skips_non_text_blocks(self):
        with patch("reader.utils.requests.post") as post_mock:
            out = translate_html_with_ollama("<figure><img src='/media/a.jpg' alt='a'></figure>")
        self.assertIn("<img", out)
        post_mock.assert_not_called()

    def test_translate_retries_then_caches(self):
        original = "<p>Hello world</p>"
        ok_response = Mock()
        ok_response.raise_for_status.return_value = None
        ok_response.json.return_value = {"response": "<div id=\"__epubdrop_root__\"><p>Hola mundo</p></div>"}

        with patch("reader.utils.requests.post", side_effect=[requests.Timeout("timeout"), ok_response]) as post_mock, patch(
            "reader.utils.time.sleep", return_value=None
        ):
            translated = translate_html_with_ollama(original)
            self.assertEqual(translated, "<p>Hola mundo</p>")
            self.assertEqual(post_mock.call_count, 2)

        self.assertEqual(TranslationCache.objects.count(), 1)
        cached = TranslationCache.objects.first()
        self.assertEqual(cached.translated_html, "<p>Hola mundo</p>")

        with patch("reader.utils.requests.post") as post_mock:
            translated_again = translate_html_with_ollama(original)
            self.assertEqual(translated_again, "<p>Hola mundo</p>")
            post_mock.assert_not_called()

    def test_translate_rejects_explanatory_failure_messages(self):
        original = "<p>Hello paragraph</p>"
        bad_response = Mock()
        bad_response.raise_for_status.return_value = None
        bad_response.json.return_value = {"response": "No puedo traducir este parrafo."}

        with patch("reader.utils.requests.post", return_value=bad_response), patch(
            "reader.utils.time.sleep", return_value=None
        ):
            translated = translate_html_with_ollama(original)
        self.assertEqual(translated, original)

    def test_translate_falls_back_if_img_is_dropped(self):
        original = "<figure><img src='/media/x.jpg' alt='x'><figcaption>Test image</figcaption></figure>"
        bad_response = Mock()
        bad_response.raise_for_status.return_value = None
        bad_response.json.return_value = {"response": "<div id=\"__epubdrop_root__\"><p>Imagen</p></div>"}

        with patch("reader.utils.requests.post", return_value=bad_response), patch(
            "reader.utils.time.sleep", return_value=None
        ):
            translated = translate_html_with_ollama(original)
        self.assertEqual(translated, original)

    def test_translate_rejects_descriptive_no_content_messages(self):
        original = "<h2>Each Running in Its Own Process</h2>"
        bad_response = Mock()
        bad_response.raise_for_status.return_value = None
        bad_response.json.return_value = {
            "response": (
                "<div id=\"__epubdrop_root__\"><p>No hay contenido HTML para traducir. "
                "El fragmento proporcionado solo contiene un encabezado.</p></div>"
            )
        }
        with patch("reader.utils.requests.post", return_value=bad_response), patch(
            "reader.utils.time.sleep", return_value=None
        ):
            translated = translate_html_with_ollama(original)
        self.assertEqual(translated, original)

    def test_translate_ignores_invalid_cached_translation(self):
        original = "<h2>A Suite of Services</h2>"
        TranslationCache.objects.create(
            content_hash=_content_hash(original),
            model_name="llama3.1",
            translated_html="<p>No hay contenido HTML para traducir.</p>",
        )

        ok_response = Mock()
        ok_response.raise_for_status.return_value = None
        ok_response.json.return_value = {
            "response": "<div id=\"__epubdrop_root__\"><h2>Una suite de servicios</h2></div>"
        }
        with patch("reader.utils.requests.post", return_value=ok_response), patch(
            "reader.utils.time.sleep", return_value=None
        ) as _:
            translated = translate_html_with_ollama(original)

        self.assertEqual(translated, "<h2>Una suite de servicios</h2>")

    def test_sanitize_book_translations_repairs_invalid_blocks(self):
        user = CustomUser.objects.create_user(email="sanitize@example.com", password="pass1234")
        book = Book.objects.create(
            owner=user,
            title="Book sanitize",
            status=Book.Status.READY,
            total_blocks=1,
            translated_blocks=1,
        )
        section = Section.objects.create(book=book, index=0)
        block = Block.objects.create(
            section=section,
            index=0,
            original_html="<h2>Built Around Business Capabilities</h2>",
            translated_html="<p>No hay contenido HTML para traducir.</p>",
        )

        with patch(
            "reader.utils.translate_html_with_ollama",
            return_value="<h2>Construido alrededor de capacidades de negocio</h2>",
        ):
            stats = sanitize_book_translations(str(book.id), force_refresh=True)

        block.refresh_from_db()
        self.assertEqual(stats["repaired"], 1)
        self.assertEqual(block.translated_html, "<h2>Construido alrededor de capacidades de negocio</h2>")


class BookmarkApiTests(TestCase):
    def setUp(self):
        self.user_a = CustomUser.objects.create_user(email="bm-a@example.com", password="pass1234")
        self.user_b = CustomUser.objects.create_user(email="bm-b@example.com", password="pass1234")
        self.book = Book.objects.create(
            owner=self.user_a,
            title="Bookmark Book",
            status=Book.Status.READY,
            total_blocks=1,
            translated_blocks=1,
        )
        section = Section.objects.create(book=self.book, index=0)
        self.block = Block.objects.create(
            section=section,
            index=0,
            original_html="<p>Original</p>",
            translated_html="<p>Traducido</p>",
        )

    def test_create_list_delete_bookmark(self):
        self.client.force_login(self.user_a)

        create_res = self.client.post(
            reverse("create_bookmark", kwargs={"book_id": str(self.book.id)}),
            data={"block_id": self.block.id, "label": "Mi marca"},
        )
        self.assertEqual(create_res.status_code, 200)
        self.assertTrue(Bookmark.objects.filter(book=self.book, user=self.user_a, block=self.block).exists())

        list_res = self.client.get(reverse("list_bookmarks", kwargs={"book_id": str(self.book.id)}))
        self.assertEqual(list_res.status_code, 200)
        payload = list_res.json()["bookmarks"]
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["label"], "Mi marca")

        bookmark_id = payload[0]["id"]
        del_res = self.client.post(
            reverse("delete_bookmark", kwargs={"book_id": str(self.book.id), "bookmark_id": bookmark_id})
        )
        self.assertEqual(del_res.status_code, 200)
        self.assertFalse(Bookmark.objects.filter(pk=bookmark_id).exists())

    def test_bookmark_api_isolation(self):
        foreign_book = Book.objects.create(
            owner=self.user_b,
            title="Foreign",
            status=Book.Status.READY,
            total_blocks=1,
            translated_blocks=1,
        )
        foreign_section = Section.objects.create(book=foreign_book, index=0)
        foreign_block = Block.objects.create(
            section=foreign_section,
            index=0,
            original_html="<p>F</p>",
            translated_html="<p>F</p>",
        )

        self.client.force_login(self.user_a)
        res = self.client.post(
            reverse("create_bookmark", kwargs={"book_id": str(foreign_book.id)}),
            data={"block_id": foreign_block.id, "label": "x"},
        )
        self.assertEqual(res.status_code, 404)


class MetricsApiTests(TestCase):
    def setUp(self):
        self.user_a = CustomUser.objects.create_user(email="metrics-a@example.com", password="pass1234")
        self.user_b = CustomUser.objects.create_user(email="metrics-b@example.com", password="pass1234")
        self.book_a1 = Book.objects.create(
            owner=self.user_a,
            title="A1",
            status=Book.Status.READY,
            total_blocks=100,
            translated_blocks=100,
        )
        self.book_a2 = Book.objects.create(
            owner=self.user_a,
            title="A2",
            status=Book.Status.TRANSLATING,
            total_blocks=50,
            translated_blocks=10,
        )
        self.book_b = Book.objects.create(
            owner=self.user_b,
            title="B",
            status=Book.Status.READY,
            total_blocks=999,
            translated_blocks=999,
        )
        section = Section.objects.create(book=self.book_a1, index=0)
        block = Block.objects.create(
            section=section,
            index=0,
            original_html="<p>x</p>",
            translated_html="<p>x</p>",
        )
        ReadingProgress.objects.create(book=self.book_a1, user=self.user_a, progress_percent=45)
        Bookmark.objects.create(book=self.book_a1, user=self.user_a, block=block, section_index=0, block_index=0)

    def test_metrics_summary_returns_user_scoped_metrics(self):
        self.client.force_login(self.user_a)
        res = self.client.get(reverse("metrics_summary"))
        self.assertEqual(res.status_code, 200)
        m = res.json()["metrics"]
        self.assertEqual(m["total_books"], 2)
        self.assertEqual(m["ready_books"], 1)
        self.assertEqual(m["translating_books"], 1)
        self.assertEqual(m["failed_books"], 0)
        self.assertEqual(m["total_blocks"], 150)
        self.assertEqual(m["translated_blocks"], 110)
        self.assertEqual(m["translated_percent"], 73)
        self.assertEqual(m["bookmarks_count"], 1)
        self.assertEqual(m["avg_reading_progress_percent"], 45.0)
