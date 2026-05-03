import re
import shutil
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Avg, F, Q, Sum
from django.utils import timezone
from lxml import html as lxml_html

from .models import Block, Book, Bookmark, ReadingProgress, Section
from .utils import (
    build_reader_sections_with_blocks_from_spine,
    extract_epub_info_from_path,
    is_valid_translation_html,
    sanitize_html_trusted,
    translate_html_with_ollama,
    validate_epub_file,
)


def build_library_context(user, query: str = "", error: Optional[str] = None, info: Optional[str] = None) -> dict:
    books = Book.objects.filter(owner=user).order_by("-created_at")
    if query:
        books = books.filter(
            Q(title__icontains=query)
            | Q(authors__icontains=query)
        )
    ready_books = books.filter(status=Book.Status.READY)
    in_progress_books = books.exclude(status=Book.Status.READY)
    return {
        "error": error,
        "info": info,
        "query": query,
        "library_books": books,
        "ready_books": ready_books,
        "in_progress_books": in_progress_books,
    }


def build_translation_progress_payload(user) -> list[dict]:
    books = Book.objects.filter(owner=user).order_by("-created_at")
    in_progress_books = books.exclude(status=Book.Status.READY)
    return [
        {
            "id": str(book.id),
            "title": book.title,
            "authors": book.authors or "Autor desconocido",
            "status": book.status,
            "progress_percent": book.progress_percent,
            "translated_blocks": book.translated_blocks,
            "total_blocks": book.total_blocks,
        }
        for book in in_progress_books
    ]


def build_metrics_summary_payload(user) -> dict:
    books = Book.objects.filter(owner=user)
    last_7_days = timezone.now() - timedelta(days=7)

    agg_blocks = books.aggregate(
        total_blocks=Sum("total_blocks"),
        translated_blocks=Sum("translated_blocks"),
    )
    total_blocks = int(agg_blocks.get("total_blocks") or 0)
    translated_blocks = int(agg_blocks.get("translated_blocks") or 0)
    translated_percent = int((translated_blocks / total_blocks) * 100) if total_blocks > 0 else 0

    avg_progress = (
        ReadingProgress.objects.filter(user=user, book__owner=user)
        .aggregate(avg=Avg("progress_percent"))
        .get("avg")
    )
    avg_progress = float(avg_progress or 0.0)

    return {
        "total_books": books.count(),
        "ready_books": books.filter(status=Book.Status.READY).count(),
        "translating_books": books.filter(status=Book.Status.TRANSLATING).count(),
        "failed_books": books.filter(status=Book.Status.FAILED).count(),
        "total_blocks": total_blocks,
        "translated_blocks": translated_blocks,
        "translated_percent": translated_percent,
        "avg_reading_progress_percent": round(avg_progress, 1),
        "bookmarks_count": Bookmark.objects.filter(user=user, book__owner=user).count(),
        "uploads_last_7_days": books.filter(created_at__gte=last_7_days).count(),
        "completed_last_7_days": books.filter(
            status=Book.Status.READY,
            updated_at__gte=last_7_days,
        ).count(),
    }


def create_book_from_upload(user, uploaded, notify_email: str) -> tuple[Optional[Book], int, Optional[str]]:
    if not uploaded:
        return None, 0, "No se recibió ningún archivo. Selecciona un .epub."

    result = validate_epub_file(uploaded)
    if not result:
        return None, 0, "Validación falló (sin detalle)."

    ok, err = result
    if not ok:
        return None, 0, err or "El archivo no es un EPUB válido."

    if notify_email:
        try:
            validate_email(notify_email)
        except ValidationError:
            return None, 0, "El correo electrónico no es válido."

    book_id = uuid.uuid4()
    book_dir = Path(settings.MEDIA_ROOT) / "epub_books" / str(book_id)
    book_dir.mkdir(parents=True, exist_ok=True)
    local_epub_path = book_dir / "book.epub"

    with open(local_epub_path, "wb") as out:
        for chunk in uploaded.chunks():
            out.write(chunk)

    info = extract_epub_info_from_path(str(local_epub_path), book_id=str(book_id))
    title = info.get("title") or "Libro"
    authors = ", ".join(info.get("authors") or [])
    description_html = info.get("description_html") or ""
    cover_url = info.get("cover_url") or ""

    sections = build_reader_sections_with_blocks_from_spine(
        local_epub_path=str(local_epub_path),
        assets_root_url=f"{settings.MEDIA_URL}epub_assets/{book_id}/",
        max_section_chars=12000,
    )

    section_titles = [(sec.get("title") or "").strip() for sec in sections]
    if any(section_titles):
        info = dict(info or {})
        info["section_titles"] = section_titles

    total_blocks = sum(len(sec.get("blocks", [])) for sec in sections)

    with transaction.atomic():
        book = Book.objects.create(
            id=book_id,
            owner=user,
            title=title,
            authors=authors,
            description_html=description_html,
            info=info,
            cover_url=cover_url,
            epub_path=f"epub_books/{book_id}/book.epub",
            notify_email=notify_email,
            status=Book.Status.TRANSLATING,
            total_blocks=total_blocks,
            translated_blocks=0,
        )

        for sec_idx, sec in enumerate(sections):
            section_obj = Section.objects.create(book=book, index=sec_idx)
            blocks = [
                Block(
                    section=section_obj,
                    index=blk_idx,
                    original_html=blk or "<p></p>",
                    translated_html="",
                )
                for blk_idx, blk in enumerate(sec.get("blocks", []))
            ]
            if blocks:
                Block.objects.bulk_create(blocks, batch_size=500)

    return book, total_blocks, None


def get_owned_book(user, book_id: str) -> Optional[Book]:
    try:
        return Book.objects.filter(pk=book_id, owner=user).first()
    except (TypeError, ValueError, ValidationError):
        return None


def _extract_section_title(blocks: list, fallback: str) -> str:
    for html in blocks:
        if not html:
            continue
        try:
            fragment = lxml_html.fragment_fromstring(html, create_parent=True)
            for tag in ["h1", "h2", "h3", "h4"]:
                el = fragment.find(".//%s" % tag)
                if el is not None:
                    text = (el.text_content() or "").strip()
                    if text:
                        return text
        except Exception:
            continue
    return fallback


def _is_generic_title(title: str) -> bool:
    text = (title or "").strip()
    if not text:
        return True
    if text.lower().startswith("sección "):
        return True
    if re.fullmatch(r"[\d\s.\-_:]+", text):
        return True
    return False


def _coerce_info_section_titles(info: dict) -> list[str]:
    raw = (info or {}).get("section_titles")
    if not isinstance(raw, list):
        return []
    return [item.strip() if isinstance(item, str) else "" for item in raw]


def _build_section_titles_from_saved_epub(book: Book) -> list[str]:
    if not book.epub_path:
        return []
    epub_file = Path(settings.MEDIA_ROOT) / book.epub_path
    if not epub_file.exists():
        return []
    try:
        sections = build_reader_sections_with_blocks_from_spine(
            local_epub_path=str(epub_file),
            assets_root_url=f"{settings.MEDIA_URL}epub_assets/{book.id}/",
            max_section_chars=12000,
        )
    except Exception:
        return []
    return [(sec.get("title") or "").strip() for sec in sections]


def build_reader_context(book: Book, user) -> dict:
    progress = ReadingProgress.objects.filter(book=book, user=user).first()
    bookmarks = (
        Bookmark.objects.filter(book=book, user=user)
        .select_related("block")
        .order_by("-created_at")
    )

    sections_out = []
    sections = list((
        Section.objects.filter(book=book)
        .prefetch_related("blocks")
        .order_by("index")
    ))

    info = dict(book.info or {})
    section_titles = _coerce_info_section_titles(info)
    if len(section_titles) < len(sections):
        inferred_titles = _build_section_titles_from_saved_epub(book)
        if inferred_titles:
            section_titles = inferred_titles
            info["section_titles"] = section_titles
            Book.objects.filter(pk=book.pk).update(info=info)
            book.info = info

    for section in sections:
        blocks_out = []
        blocks_list = list(section.blocks.all().order_by("index"))
        for block in blocks_list:
            has_translation = bool((block.translated_html or "").strip())
            translated_html = block.translated_html
            if has_translation:
                translated_html = sanitize_html_trusted(translated_html)
                if translated_html != block.translated_html:
                    Block.objects.filter(pk=block.pk).update(translated_html=translated_html)
                has_translation = bool(translated_html.strip())
            blocks_out.append({
                "id": block.id,
                "original_html": block.original_html,
                "translated_html": translated_html if has_translation else block.original_html,
                "has_translation": has_translation,
            })

        fallback_title = "Sección %d" % (section.index + 1)
        inferred_title = _extract_section_title([b.original_html for b in blocks_list], fallback=fallback_title)
        saved_title = section_titles[section.index] if section.index < len(section_titles) else ""
        title = saved_title or inferred_title
        if _is_generic_title(title) and saved_title:
            title = saved_title
        sections_out.append({
            "blocks": blocks_out,
            "title": title or fallback_title,
        })

    toc_entries = [
        {
            "section_index": idx,
            "title": sec.get("title") or f"Sección {idx + 1}",
        }
        for idx, sec in enumerate(sections_out)
    ]

    translation_enabled = not bool((book.info or {}).get("translation_disabled"))

    return {
        "book_id": str(book.id),
        "title": book.title,
        "info": book.info or {},
        "sections": sections_out,
        "toc_entries": toc_entries,
        "bookmarks": [
            {
                "id": bm.id,
                "block_id": bm.block_id,
                "section_index": bm.section_index,
                "block_index": bm.block_index,
                "label": bm.label or f"Sección {bm.section_index + 1}, bloque {bm.block_index + 1}",
            }
            for bm in bookmarks
        ],
        "translation_enabled": translation_enabled,
        "saved_progress": {
            "section_index": progress.section_index if progress else 0,
            "block_index": progress.block_index if progress else 0,
            "block_offset_percent": progress.block_offset_percent if progress else 0,
            "block_id": progress.block_id if progress else 0,
            "anchor_text": progress.anchor_text if progress else "",
            "anchor_char_index": progress.anchor_char_index if progress else 0,
        },
    }


def translate_block_for_user(user, book_id: str, section_idx: int, block_idx: int) -> dict:
    book = get_owned_book(user, book_id)
    if not book:
        return {"ok": True, "translated_html": "<p></p>", "fallback": "missing_book"}

    if (book.info or {}).get("translation_disabled"):
        return {"ok": True, "translated_html": "<p></p>", "fallback": "translation_disabled"}

    section = Section.objects.filter(book=book, index=section_idx).first()
    if not section:
        return {"ok": True, "translated_html": "<p></p>", "fallback": "section_oob"}

    block = Block.objects.filter(section=section, index=block_idx).first()
    if not block:
        return {"ok": True, "translated_html": "<p></p>", "fallback": "block_oob"}

    original_html = block.original_html or "<p></p>"
    if block.translated_html:
        translated_html = sanitize_html_trusted(block.translated_html)
        if translated_html != block.translated_html:
            Block.objects.filter(pk=block.pk).update(translated_html=translated_html)
        if translated_html.strip():
            return {
                "ok": True,
                "translated_html": translated_html,
                "cached": True,
                "_log_event": "book.block.translate.cache_hit",
            }

    try:
        translated = (translate_html_with_ollama(original_html) or "").strip()
        if not translated or not is_valid_translation_html(original_html, translated):
            return {
                "ok": True,
                "translated_html": original_html,
                "fallback": "invalid_translation",
                "_log_event": "book.block.translate.invalid_fallback",
            }

        updated = Block.objects.filter(pk=block.pk, translated_html="").update(translated_html=translated)
        if updated:
            Book.objects.filter(pk=book_id).update(
                translated_blocks=F("translated_blocks") + 1,
                updated_at=timezone.now(),
            )
            Book.objects.filter(
                pk=book_id,
                status=Book.Status.TRANSLATING,
                total_blocks__gt=0,
                translated_blocks__gte=F("total_blocks"),
            ).update(status=Book.Status.READY, error_message="", updated_at=timezone.now())
        else:
            translated = Block.objects.filter(pk=block.pk).values_list("translated_html", flat=True).first() or translated

        return {
            "ok": True,
            "translated_html": translated,
            "saved": bool(updated),
            "_log_event": "book.block.translate.generated",
        }
    except Exception:
        return {
            "ok": True,
            "translated_html": original_html,
            "fallback": "translate_failed",
            "_log_event": "book.block.translate.fallback",
        }


def save_progress_for_user(user, book_id: str, post_data) -> tuple[dict, int, Optional[dict]]:
    book = get_owned_book(user, book_id)
    if not book:
        return {"ok": False, "error": "missing_book"}, 404, None

    try:
        section_idx = int(post_data.get("section_idx", "0"))
        block_idx = int(post_data.get("block_idx", "0"))
        progress_percent = int(post_data.get("progress_percent", "0"))
        block_offset_percent = float(post_data.get("block_offset_percent", "0"))
        block_id = int(post_data.get("block_id", "0"))
        anchor_char_index = int(post_data.get("anchor_char_index", "0"))
    except ValueError:
        return {"ok": False, "error": "invalid_payload"}, 400, None

    anchor_text = (post_data.get("anchor_text") or "").strip()[:240]

    block_obj = None
    if block_id > 0:
        block_obj = Block.objects.filter(pk=block_id, section__book=book).first()
        if block_obj:
            section_idx = block_obj.section.index
            block_idx = block_obj.index

    ReadingProgress.objects.update_or_create(
        book=book,
        user=user,
        defaults={
            "block": block_obj,
            "section_index": max(0, section_idx),
            "block_index": max(0, block_idx),
            "block_offset_percent": max(0.0, min(1.0, block_offset_percent)),
            "anchor_text": anchor_text,
            "anchor_char_index": max(0, anchor_char_index),
            "progress_percent": max(0, min(100, progress_percent)),
        },
    )
    log_payload = {
        "section_idx": section_idx,
        "block_idx": block_idx,
        "progress_percent": progress_percent,
    }
    return {"ok": True}, 200, log_payload


def clear_book_for_user(user, book_id: str) -> bool:
    book = get_owned_book(user, book_id)
    if not book:
        return False

    book_dir = Path(settings.MEDIA_ROOT) / "epub_books" / book_id
    assets_dir = Path(settings.MEDIA_ROOT) / "epub_assets" / book_id

    if book_dir.exists():
        shutil.rmtree(book_dir, ignore_errors=True)
    if assets_dir.exists():
        shutil.rmtree(assets_dir, ignore_errors=True)

    book.delete()
    return True


def list_bookmarks_for_user(user, book_id: str) -> tuple[dict, int]:
    book = get_owned_book(user, book_id)
    if not book:
        return {"ok": False, "error": "missing_book"}, 404

    bookmarks = Bookmark.objects.filter(book=book, user=user).order_by("-created_at")
    return {
        "ok": True,
        "bookmarks": [
            {
                "id": bm.id,
                "block_id": bm.block_id,
                "section_index": bm.section_index,
                "block_index": bm.block_index,
                "label": bm.label or f"Sección {bm.section_index + 1}, bloque {bm.block_index + 1}",
                "created_at": bm.created_at.isoformat(),
            }
            for bm in bookmarks
        ],
    }, 200


def create_bookmark_for_user(user, book_id: str, post_data) -> tuple[dict, int]:
    book = get_owned_book(user, book_id)
    if not book:
        return {"ok": False, "error": "missing_book"}, 404

    try:
        block_id = int(post_data.get("block_id", "0"))
    except ValueError:
        return {"ok": False, "error": "invalid_payload"}, 400

    block = Block.objects.filter(pk=block_id, section__book=book).select_related("section").first()
    if not block:
        return {"ok": False, "error": "missing_block"}, 404

    label = (post_data.get("label") or "").strip()[:200]
    bm, _created = Bookmark.objects.get_or_create(
        book=book,
        user=user,
        block=block,
        defaults={
            "section_index": block.section.index,
            "block_index": block.index,
            "label": label,
        },
    )
    if label and bm.label != label:
        bm.label = label
        bm.save(update_fields=["label"])

    return {
        "ok": True,
        "bookmark": {
            "id": bm.id,
            "block_id": bm.block_id,
            "section_index": bm.section_index,
            "block_index": bm.block_index,
            "label": bm.label or f"Sección {bm.section_index + 1}, bloque {bm.block_index + 1}",
        },
    }, 200


def delete_bookmark_for_user(user, book_id: str, bookmark_id: int) -> tuple[dict, int]:
    book = get_owned_book(user, book_id)
    if not book:
        return {"ok": False, "error": "missing_book"}, 404

    deleted, _ = Bookmark.objects.filter(
        pk=bookmark_id,
        book=book,
        user=user,
    ).delete()
    if not deleted:
        return {"ok": False, "error": "missing_bookmark"}, 404

    return {"ok": True}, 200
