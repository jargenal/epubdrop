import shutil
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

import requests

from reader.models import Block, Book, Bookmark, CustomUser, ReadingProgress, Section, TranslationCache
from reader.tasks import _translate_book, _TranslationRunGuard, prepare_book_for_translation
from reader.utils import (
    _content_hash,
    is_valid_translation_html,
    sanitize_book_translations,
    sanitize_html_trusted,
    translate_html_with_ollama,
)


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

    def test_library_cards_include_client_sort_metadata(self):
        book = self._make_ready_book(owner=self.user_a, title="Árbol de prueba")
        ReadingProgress.objects.create(book=book, user=self.user_a, progress_percent=12)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("upload_epub"))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn('id="librarySort"', body)
        self.assertIn('value="title-asc">Alfabético (A-Z)</option>', body)
        self.assertIn('value="title-desc">Alfabético (Z-A)</option>', body)
        self.assertIn('value="recent-read">Recién leído</option>', body)
        self.assertIn('value="added-desc">Fecha de agregado</option>', body)
        self.assertIn('data-sort-title="Árbol de prueba"', body)
        self.assertRegex(body, r'data-added-at="[^"]+"')
        self.assertRegex(body, r'data-read-at="[^"]+"')

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

    def test_saved_progress_is_available_from_a_separate_browser_session(self):
        own_book = self._make_ready_book(owner=self.user_a)
        section = Section.objects.create(book=own_book, index=1)
        block = Block.objects.create(
            section=section,
            index=2,
            original_html="<p>Original avanzado</p>",
            translated_html="<p>Traducido avanzado</p>",
        )
        safari = Client()
        chrome = Client()
        safari.force_login(self.user_a)
        chrome.force_login(self.user_a)

        save_response = safari.post(
            reverse("save_progress", kwargs={"book_id": str(own_book.id)}),
            data={
                "section_idx": 0,
                "block_idx": 0,
                "progress_percent": 64,
                "block_offset_percent": 0.62,
                "block_id": block.id,
                "anchor_text": "Original avanzado",
                "anchor_char_index": 9,
            },
        )
        self.assertEqual(save_response.status_code, 200)

        rp = ReadingProgress.objects.get(book=own_book, user=self.user_a)
        self.assertEqual(rp.block_id, block.id)
        self.assertEqual(rp.section_index, 1)
        self.assertEqual(rp.block_index, 2)
        self.assertEqual(rp.progress_percent, 64)
        self.assertAlmostEqual(rp.block_offset_percent, 0.62, places=3)
        self.assertEqual(rp.anchor_text, "Original avanzado")
        self.assertEqual(rp.anchor_char_index, 9)

        read_response = chrome.get(reverse("read_book", kwargs={"book_id": str(own_book.id)}))
        self.assertEqual(read_response.status_code, 200)
        self.assertContains(read_response, f'data-book-id="{own_book.id}"')
        self.assertNotContains(read_response, "\\u002D")
        self.assertContains(read_response, 'data-saved-section="1"')
        self.assertContains(read_response, 'data-saved-block="2"')
        self.assertContains(read_response, 'data-saved-offset="0.62"')
        self.assertContains(read_response, f'data-saved-block-id="{block.id}"')
        self.assertContains(read_response, 'data-saved-anchor-text="Original avanzado"')
        self.assertContains(read_response, 'data-saved-anchor-char="9"')

    def test_translate_block_with_invalid_uuid_returns_missing_book_fallback(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            "/api/books/1549703c\\u002D4481\\u002D47e7\\u002Dade7\\u002D5bf51adc8e1b/translate-block/0/0/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["fallback"], "missing_book")

    def test_translation_disabled_book_does_not_translate_lazy_blocks(self):
        book = self._make_ready_book(owner=self.user_a, status=Book.Status.TRANSLATING)
        book.info = {"translation_disabled": True}
        book.save(update_fields=["info"])
        Block.objects.filter(section__book=book).update(translated_html="")

        self.client.force_login(self.user_a)
        read_response = self.client.get(reverse("read_book", kwargs={"book_id": str(book.id)}))
        self.assertEqual(read_response.status_code, 200)
        self.assertContains(read_response, 'data-translation-enabled="0"')

        with patch("reader.services.translate_html_with_ollama") as translate_mock:
            response = self.client.get(
                reverse("translate_block", kwargs={"book_id": str(book.id), "section_idx": 0, "block_idx": 0})
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["fallback"], "translation_disabled")
        translate_mock.assert_not_called()

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

    def test_translate_block_uses_existing_translation_without_ollama(self):
        book = self._make_ready_book(owner=self.user_a)

        self.client.force_login(self.user_a)
        with patch("reader.services.translate_html_with_ollama") as translate_mock:
            response = self.client.get(
                reverse("translate_block", kwargs={"book_id": str(book.id), "section_idx": 0, "block_idx": 0})
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["translated_html"], "<p>Traducido</p>")
        self.assertTrue(response.json()["cached"])
        translate_mock.assert_not_called()

    def test_translate_block_saves_valid_translation_and_counts_once(self):
        book = Book.objects.create(
            owner=self.user_a,
            title="Lazy book",
            status=Book.Status.TRANSLATING,
            total_blocks=1,
            translated_blocks=0,
        )
        section = Section.objects.create(book=book, index=0)
        Block.objects.create(
            section=section,
            index=0,
            original_html="<p>Hello world again</p>",
            translated_html="",
        )

        self.client.force_login(self.user_a)
        with patch("reader.services.translate_html_with_ollama", return_value="<p>Hola mundo de nuevo</p>") as translate_mock:
            first = self.client.get(
                reverse("translate_block", kwargs={"book_id": str(book.id), "section_idx": 0, "block_idx": 0})
            )
            second = self.client.get(
                reverse("translate_block", kwargs={"book_id": str(book.id), "section_idx": 0, "block_idx": 0})
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(translate_mock.call_count, 1)
        book.refresh_from_db()
        self.assertEqual(book.translated_blocks, 1)
        self.assertEqual(book.status, Book.Status.READY)

    def test_translate_block_invalid_translation_falls_back_without_counting(self):
        book = Book.objects.create(
            owner=self.user_a,
            title="Fallback book",
            status=Book.Status.TRANSLATING,
            total_blocks=1,
            translated_blocks=0,
        )
        section = Section.objects.create(book=book, index=0)
        block = Block.objects.create(
            section=section,
            index=0,
            original_html="<p>Hello world again</p>",
            translated_html="",
        )

        self.client.force_login(self.user_a)
        with patch("reader.services.translate_html_with_ollama", return_value="<p>Hello world again</p>"):
            response = self.client.get(
                reverse("translate_block", kwargs={"book_id": str(book.id), "section_idx": 0, "block_idx": 0})
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["fallback"], "invalid_translation")
        block.refresh_from_db()
        book.refresh_from_db()
        self.assertEqual(block.translated_html, "")
        self.assertEqual(book.translated_blocks, 0)

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
            with patch("reader.services.validate_epub_file", return_value=(True, None)), patch(
                "reader.services.extract_epub_info_from_path",
                return_value={"title": "Mock Book", "authors": ["Author"], "description_html": "", "cover_url": ""},
            ), patch(
                "reader.services.build_reader_sections_with_blocks_from_spine",
                return_value=[{"id": "s0", "blocks": ["<p>uno</p>"]}],
            ), patch("reader.views.start_book_translation_async") as start_translation_mock:
                response = self.client.post(
                    reverse("upload_epub"),
                    data={"epub_file": epub_file, "notify_email": "owner@example.com"},
                )

        self.assertEqual(response.status_code, 302)
        created = Book.objects.get(title="Mock Book")
        self.assertEqual(created.owner_id, self.user_a.id)
        self.assertEqual(created.status, Book.Status.TRANSLATING)
        self.assertEqual(created.translated_blocks, 0)
        start_translation_mock.assert_called_once_with(str(created.id))

    def test_upload_persists_section_titles_from_builder(self):
        epub_file = SimpleUploadedFile(
            "test.epub",
            b"PK\x03\x04fake-epub",
            content_type="application/epub+zip",
        )
        with self.settings(MEDIA_ROOT=self.media_root):
            self.client.force_login(self.user_a)
            with patch("reader.services.validate_epub_file", return_value=(True, None)), patch(
                "reader.services.extract_epub_info_from_path",
                return_value={"title": "Mock Book", "authors": ["Author"], "description_html": "", "cover_url": ""},
            ), patch(
                "reader.services.build_reader_sections_with_blocks_from_spine",
                return_value=[
                    {"id": "s0", "blocks": ["<p>uno</p>"], "title": "Capítulo 1"},
                    {"id": "s1", "blocks": ["<p>dos</p>"], "title": "Capítulo 2"},
                ],
            ), patch("reader.views.start_book_translation_async"):
                response = self.client.post(
                    reverse("upload_epub"),
                    data={"epub_file": epub_file, "notify_email": "owner@example.com"},
                )

        self.assertEqual(response.status_code, 302)
        created = Book.objects.get(title="Mock Book")
        self.assertEqual(created.info.get("section_titles"), ["Capítulo 1", "Capítulo 2"])

    def test_read_book_allows_translating_book_with_original_content(self):
        book = self._make_ready_book(owner=self.user_a, status=Book.Status.TRANSLATING)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("read_book", kwargs={"book_id": str(book.id)}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<p>Original</p>", html=True)
        self.assertContains(response, 'data-translation-state="ready"')

    def test_read_book_sanitizes_persisted_translated_html(self):
        book = self._make_ready_book(owner=self.user_a)
        block = Block.objects.filter(section__book=book).first()
        block.translated_html = "<p onclick='alert(1)'>Traducido</p>"
        block.save(update_fields=["translated_html"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("read_book", kwargs={"book_id": str(book.id)}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<p>Traducido</p>", html=True)
        self.assertNotContains(response, "onclick")

    def test_read_book_uses_saved_section_titles_over_numeric_headings(self):
        book = Book.objects.create(
            owner=self.user_a,
            title="Libro con toc",
            status=Book.Status.READY,
            total_blocks=2,
            translated_blocks=2,
            info={"section_titles": ["Introducción clara", "Diseño de datos"]},
        )
        section_0 = Section.objects.create(book=book, index=0)
        section_1 = Section.objects.create(book=book, index=1)
        Block.objects.create(section=section_0, index=0, original_html="<h1>1</h1>", translated_html="")
        Block.objects.create(section=section_1, index=0, original_html="<h1>2</h1>", translated_html="")

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("read_book", kwargs={"book_id": str(book.id)}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Introducción clara")
        self.assertContains(response, "Diseño de datos")


class TranslationQualityTests(TestCase):
    @staticmethod
    def _mock_translation_response(html_fragment: str) -> Mock:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"response": f"<div id=\"__epubdrop_root__\">{html_fragment}</div>"}
        return response

    def test_sanitize_html_removes_active_content_and_unsafe_links(self):
        cleaned = sanitize_html_trusted(
            """
            <div onclick="alert(1)">
              <script>alert(1)</script>
              <img src="/media/a.jpg" onerror="alert(2)" width="120" height="bad">
              <a href="javascript:alert(3)">bad js</a>
              <a href="data:text/html;base64,PHNjcmlwdD4=">bad data</a>
            </div>
            """
        )

        self.assertNotIn("<script", cleaned.lower())
        self.assertNotIn("onclick", cleaned.lower())
        self.assertNotIn("onerror", cleaned.lower())
        self.assertNotIn("javascript:", cleaned.lower())
        self.assertNotIn("data:text/html", cleaned.lower())
        self.assertIn('src="/media/a.jpg"', cleaned)
        self.assertIn('width="120"', cleaned)
        self.assertNotIn('height="bad"', cleaned)

    def test_sanitize_html_allows_safe_data_images_only(self):
        cleaned = sanitize_html_trusted(
            """
            <img src="data:image/png;base64,iVBORw0KGgo=" alt="png">
            <img src="data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+" alt="svg">
            """
        )

        self.assertIn("data:image/png;base64,iVBORw0KGgo=", cleaned)
        self.assertNotIn("data:image/svg+xml", cleaned)

    def test_translate_skips_non_text_blocks(self):
        with patch("reader.utils.requests.post") as post_mock:
            out = translate_html_with_ollama("<figure><img src='/media/a.jpg' alt='a'></figure>")
        self.assertIn("<img", out)
        post_mock.assert_not_called()

    def test_translate_retries_then_caches(self):
        original = "<p>Hello world</p>"
        ok_response = self._mock_translation_response("<p>Hola mundo</p>")

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

        ok_response = self._mock_translation_response("<h2>Una suite de servicios</h2>")
        with patch("reader.utils.requests.post", return_value=ok_response), patch(
            "reader.utils.time.sleep", return_value=None
        ) as _:
            translated = translate_html_with_ollama(original)

        self.assertEqual(translated, "<h2>Una suite de servicios</h2>")

    def test_translate_sanitizes_valid_cached_translation_before_returning(self):
        original = "<figure><img src='/media/x.jpg' alt='x'><figcaption>Test image</figcaption></figure>"
        cache = TranslationCache.objects.create(
            content_hash=_content_hash(original),
            model_name="translategemma:4b",
            translated_html=(
                "<figure><img src='/media/x.jpg' alt='x' onerror='alert(1)'>"
                "<figcaption>Imagen de prueba</figcaption></figure>"
            ),
        )

        with patch("reader.utils.requests.post") as post_mock:
            translated = translate_html_with_ollama(original)

        post_mock.assert_not_called()
        self.assertNotIn("onerror", translated.lower())
        cache.refresh_from_db()
        self.assertNotIn("onerror", cache.translated_html.lower())

    def test_translate_sanitizes_model_response_before_caching(self):
        original = "<figure><img src='/media/x.jpg' alt='x'><figcaption>Test image</figcaption></figure>"
        response = self._mock_translation_response(
            "<figure><img src='/media/x.jpg' alt='x' onerror='alert(1)'>"
            "<figcaption>Imagen de prueba</figcaption></figure>"
        )

        with patch("reader.utils.requests.post", return_value=response), patch(
            "reader.utils.time.sleep", return_value=None
        ):
            translated = translate_html_with_ollama(original, force_refresh=True)

        self.assertNotIn("onerror", translated.lower())
        cached = TranslationCache.objects.get(content_hash=_content_hash(original))
        self.assertNotIn("onerror", cached.translated_html.lower())

    def test_translate_retries_when_a_segment_stays_in_english(self):
        original = "<div><p>Hello team</p><p>Make the right trade-offs</p></div>"
        partially_translated = self._mock_translation_response(
            "<div><p>Hola equipo</p><p>Make the right trade-offs</p></div>"
        )
        fixed_translation = self._mock_translation_response(
            "<div><p>Hola equipo</p><p>Haz las compensaciones correctas</p></div>"
        )

        with patch("reader.utils.requests.post", side_effect=[partially_translated, fixed_translation]) as post_mock, patch(
            "reader.utils.time.sleep", return_value=None
        ):
            translated = translate_html_with_ollama(original, force_refresh=True)

        self.assertEqual(
            translated,
            "<div><p>Hola equipo</p><p>Haz las compensaciones correctas</p></div>",
        )
        self.assertEqual(post_mock.call_count, 2)

    def test_translate_rejects_html_comments_from_model(self):
        original = "<p>Hello paragraph</p>"
        bad_response = self._mock_translation_response("<p>Hola parrafo</p><!-- untranslated paragraph -->")

        with patch("reader.utils.requests.post", return_value=bad_response), patch(
            "reader.utils.time.sleep", return_value=None
        ):
            translated = translate_html_with_ollama(original, force_refresh=True)

        self.assertEqual(translated, original)

    def test_invalid_translation_html_detects_unchanged_english_segment(self):
        original = "<div><p>Hello team</p><p>Give good feedback</p></div>"
        translated = "<div><p>Hola equipo</p><p>Give good feedback</p></div>"

        self.assertFalse(is_valid_translation_html(original, translated))

    def test_translate_applies_configured_request_cooldown(self):
        original = "<p>Hello paragraph</p>"

        with self.settings(TRANSLATION_REQUEST_COOLDOWN_SECONDS=0.25):
            with patch(
                "reader.utils.requests.post",
                return_value=self._mock_translation_response("<p>Hola parrafo</p>"),
            ), patch("reader.utils.time.sleep", return_value=None) as sleep_mock:
                translated = translate_html_with_ollama(original, force_refresh=True)

        self.assertEqual(translated, "<p>Hola parrafo</p>")
        sleep_mock.assert_called_once_with(0.25)

    def test_translate_limits_concurrent_requests(self):
        first_request_started = threading.Event()
        second_request_started = threading.Event()
        release_first_request = threading.Event()
        call_lock = threading.Lock()
        calls = {"count": 0}
        results = {}

        def fake_post(*args, **kwargs):
            with call_lock:
                calls["count"] += 1
                call_number = calls["count"]

            if call_number == 1:
                first_request_started.set()
                if not release_first_request.wait(timeout=2):
                    raise AssertionError("first request did not finish in time")
                return self._mock_translation_response("<p>Uno</p>")

            second_request_started.set()
            return self._mock_translation_response("<p>Dos</p>")

        def run_translation(name: str, html: str) -> None:
            results[name] = translate_html_with_ollama(html, force_refresh=True)

        with self.settings(TRANSLATION_MAX_CONCURRENT_REQUESTS=1, TRANSLATION_REQUEST_COOLDOWN_SECONDS=0):
            cache_qs = Mock()
            cache_qs.first.return_value = None
            with patch("reader.utils.TranslationCache.objects.filter", return_value=cache_qs), patch(
                "reader.utils.TranslationCache.objects.update_or_create",
                return_value=(None, True),
            ), patch("reader.utils.requests.post", side_effect=fake_post):
                t1 = threading.Thread(target=run_translation, args=("first", "<p>First</p>"))
                t2 = threading.Thread(target=run_translation, args=("second", "<p>Second</p>"))
                t1.start()
                self.assertTrue(first_request_started.wait(timeout=1))
                t2.start()
                self.assertFalse(second_request_started.wait(timeout=0.2))
                release_first_request.set()
                t1.join(timeout=2)
                t2.join(timeout=2)

        self.assertEqual(results["first"], "<p>Uno</p>")
        self.assertEqual(results["second"], "<p>Dos</p>")
        self.assertTrue(second_request_started.is_set())

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


class TranslationTaskTests(TestCase):
    def test_translation_run_guard_throttles_run_id_checks(self):
        guard = _TranslationRunGuard(
            "book-id",
            "run-id",
            check_every_blocks=3,
            max_interval_seconds=60,
        )

        with patch("reader.tasks._translation_run_is_current", return_value=True) as current_mock:
            self.assertTrue(guard.is_current(force=True))
            guard.mark_block_processed()
            self.assertTrue(guard.is_current())
            guard.mark_block_processed()
            self.assertTrue(guard.is_current())
            guard.mark_block_processed()
            self.assertTrue(guard.is_current())

        self.assertEqual(current_mock.call_count, 2)

    def test_prepare_book_for_translation_resets_blocks_and_assigns_run_id(self):
        user = CustomUser.objects.create_user(email="prep@example.com", password="pass1234")
        book = Book.objects.create(
            owner=user,
            title="Prep book",
            status=Book.Status.FAILED,
            total_blocks=1,
            translated_blocks=1,
            info={"section_titles": ["Intro"], "translation_disabled": True},
            error_message="boom",
        )
        section = Section.objects.create(book=book, index=0)
        block = Block.objects.create(
            section=section,
            index=0,
            original_html="<p>The quick brown fox jumps over the lazy dog</p>",
            translated_html="<p>Hola mundo</p>",
        )

        run_id = prepare_book_for_translation(str(book.id), reset_blocks=True)

        self.assertTrue(run_id)
        block.refresh_from_db()
        book.refresh_from_db()
        self.assertEqual(block.translated_html, "")
        self.assertEqual(book.status, Book.Status.TRANSLATING)
        self.assertEqual(book.translated_blocks, 0)
        self.assertEqual(book.error_message, "")
        self.assertEqual(book.info.get("section_titles"), ["Intro"])
        self.assertEqual(book.info.get("translation_run_id"), run_id)
        self.assertNotIn("translation_disabled", book.info)

    def test_translate_book_does_not_run_full_sanitize_pass_after_translation(self):
        user = CustomUser.objects.create_user(email="task@example.com", password="pass1234")
        book = Book.objects.create(
            owner=user,
            title="Task book",
            status=Book.Status.TRANSLATING,
            total_blocks=1,
            translated_blocks=0,
        )
        section = Section.objects.create(book=book, index=0)
        Block.objects.create(
            section=section,
            index=0,
            original_html="<p>The quick brown fox jumps over the lazy dog</p>",
            translated_html="",
        )

        with patch("reader.tasks.translate_html_with_ollama", return_value="<p>Hola mundo</p>"), patch(
            "reader.tasks.sanitize_book_translations", create=True
        ) as sanitize_mock, patch("reader.tasks._send_completion_email", return_value=None):
            _translate_book(str(book.id))

        sanitize_mock.assert_not_called()
        book.refresh_from_db()
        self.assertEqual(book.status, Book.Status.READY)
        self.assertEqual(book.translated_blocks, 1)

    def test_translate_book_does_not_save_invalid_fallback_as_translation(self):
        user = CustomUser.objects.create_user(email="invalid-task@example.com", password="pass1234")
        book = Book.objects.create(
            owner=user,
            title="Invalid task book",
            status=Book.Status.TRANSLATING,
            total_blocks=1,
            translated_blocks=0,
        )
        section = Section.objects.create(book=book, index=0)
        block = Block.objects.create(
            section=section,
            index=0,
            original_html="<p>The quick brown fox jumps over the lazy dog</p>",
            translated_html="",
        )

        with patch("reader.tasks.translate_html_with_ollama", return_value="<p>The quick brown fox jumps over the lazy dog</p>"), patch(
            "reader.tasks._send_completion_email", return_value=None
        ):
            _translate_book(str(book.id))

        block.refresh_from_db()
        book.refresh_from_db()
        self.assertEqual(block.translated_html, "")
        self.assertEqual(book.translated_blocks, 0)
        self.assertEqual(book.status, Book.Status.TRANSLATING)
        self.assertIn("Algunos bloques", book.error_message)

    def test_translate_book_reuses_cached_run_check_across_multiple_blocks(self):
        user = CustomUser.objects.create_user(email="cached@example.com", password="pass1234")
        book = Book.objects.create(
            owner=user,
            title="Cached run checks",
            status=Book.Status.TRANSLATING,
            total_blocks=3,
            translated_blocks=0,
        )
        section = Section.objects.create(book=book, index=0)
        for idx in range(3):
            Block.objects.create(
                section=section,
                index=idx,
                original_html=f"<p>Hello world {idx}</p>",
                translated_html="",
            )

        run_id = prepare_book_for_translation(str(book.id), reset_blocks=False)

        with patch("reader.tasks.translate_html_with_ollama", return_value="<p>Hola mundo</p>"), patch(
            "reader.tasks._translation_run_is_current", return_value=True
        ) as current_mock, patch("reader.tasks._send_completion_email", return_value=None):
            _translate_book(str(book.id), run_id=run_id)

        self.assertEqual(current_mock.call_count, 2)

    def test_translate_book_stops_when_run_is_superseded(self):
        user = CustomUser.objects.create_user(email="supersede@example.com", password="pass1234")
        book = Book.objects.create(
            owner=user,
            title="Superseded book",
            status=Book.Status.TRANSLATING,
            total_blocks=1,
            translated_blocks=0,
        )
        section = Section.objects.create(book=book, index=0)
        block = Block.objects.create(
            section=section,
            index=0,
            original_html="<p>Hello world</p>",
            translated_html="",
        )

        current_run_id = prepare_book_for_translation(str(book.id), reset_blocks=False)
        stale_run_id = "stale-run-id"

        with patch("reader.tasks.translate_html_with_ollama", return_value="<p>Hola mundo</p>"), patch(
            "reader.tasks._send_completion_email", return_value=None
        ):
            _translate_book(str(book.id), run_id=stale_run_id)

        block.refresh_from_db()
        book.refresh_from_db()
        self.assertEqual(block.translated_html, "")
        self.assertEqual(book.info.get("translation_run_id"), current_run_id)
        self.assertEqual(book.translated_blocks, 0)


class RestartTranslationCommandTests(TestCase):
    def test_restart_translation_command_restarts_book_with_current_code(self):
        user = CustomUser.objects.create_user(email="cmd@example.com", password="pass1234")
        book = Book.objects.create(
            owner=user,
            title="Cmd book",
            status=Book.Status.TRANSLATING,
            total_blocks=1,
            translated_blocks=1,
        )
        section = Section.objects.create(book=book, index=0)
        block = Block.objects.create(
            section=section,
            index=0,
            original_html="<p>Hello world</p>",
            translated_html="<p>Viejo</p>",
        )

        with patch("reader.tasks.translate_html_with_ollama", return_value="<p>Hola mundo</p>"), patch(
            "reader.tasks._send_completion_email", return_value=None
        ):
            call_command("restart_translation", book_id=str(book.id))

        block.refresh_from_db()
        book.refresh_from_db()
        self.assertEqual(block.translated_html, "<p>Hola mundo</p>")
        self.assertEqual(book.status, Book.Status.READY)
        self.assertEqual(book.translated_blocks, 1)


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
