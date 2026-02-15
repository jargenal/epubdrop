import shutil
import uuid
import logging
from datetime import timedelta
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Avg, Q, Sum
from django.http import Http404, JsonResponse
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from lxml import html as lxml_html

from .models import Block, Book, Bookmark, ReadingProgress, Section
from .logging_utils import log_event
from .tasks import start_book_translation_async
from .utils import (
    validate_epub_file,
    extract_epub_info_from_path,
    build_reader_sections_with_blocks_from_spine,
    translate_html_with_ollama,
)

logger = logging.getLogger(__name__)


def _library_context(user, query: str = "", error: Optional[str] = None, info: Optional[str] = None) -> dict:
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
        "ready_books": ready_books,
        "in_progress_books": in_progress_books,
    }


@require_GET
@login_required
def translation_progress(request):
    books = Book.objects.filter(owner=request.user).order_by("-created_at")
    in_progress_books = books.exclude(status=Book.Status.READY)
    payload = []
    for book in in_progress_books:
        payload.append({
            "id": str(book.id),
            "title": book.title,
            "authors": book.authors or "Autor desconocido",
            "status": book.status,
            "progress_percent": book.progress_percent,
            "translated_blocks": book.translated_blocks,
            "total_blocks": book.total_blocks,
        })
    log_event(
        logger,
        logging.INFO,
        "library.translation_progress",
        user_id=request.user.id,
        in_progress=len(payload),
    )
    return JsonResponse({"in_progress": payload})


@require_GET
@login_required
def metrics_summary(request):
    books = Book.objects.filter(owner=request.user)
    now = timezone.now()
    last_7_days = now - timedelta(days=7)

    total_books = books.count()
    ready_books = books.filter(status=Book.Status.READY).count()
    translating_books = books.filter(status=Book.Status.TRANSLATING).count()
    failed_books = books.filter(status=Book.Status.FAILED).count()

    agg_blocks = books.aggregate(
        total_blocks=Sum("total_blocks"),
        translated_blocks=Sum("translated_blocks"),
    )
    total_blocks = int(agg_blocks.get("total_blocks") or 0)
    translated_blocks = int(agg_blocks.get("translated_blocks") or 0)
    translated_percent = int((translated_blocks / total_blocks) * 100) if total_blocks > 0 else 0

    avg_progress = (
        ReadingProgress.objects.filter(user=request.user, book__owner=request.user)
        .aggregate(avg=Avg("progress_percent"))
        .get("avg")
    )
    avg_progress = float(avg_progress or 0.0)

    bookmarks_count = Bookmark.objects.filter(user=request.user, book__owner=request.user).count()
    uploads_last_7_days = books.filter(created_at__gte=last_7_days).count()
    completed_last_7_days = books.filter(
        status=Book.Status.READY,
        updated_at__gte=last_7_days,
    ).count()

    payload = {
        "total_books": total_books,
        "ready_books": ready_books,
        "translating_books": translating_books,
        "failed_books": failed_books,
        "total_blocks": total_blocks,
        "translated_blocks": translated_blocks,
        "translated_percent": translated_percent,
        "avg_reading_progress_percent": round(avg_progress, 1),
        "bookmarks_count": bookmarks_count,
        "uploads_last_7_days": uploads_last_7_days,
        "completed_last_7_days": completed_last_7_days,
    }
    log_event(
        logger,
        logging.INFO,
        "library.metrics.summary",
        user_id=request.user.id,
        payload=payload,
    )
    return JsonResponse({"ok": True, "metrics": payload}, status=200)


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


@require_http_methods(["GET", "POST"])
@login_required
def upload_epub(request):
    """
    GET:
      - Biblioteca con búsqueda y sección de carga

    POST:
      - Valida EPUB
      - Guarda a disco (streaming)
      - Extrae assets + metadata (incluye cover_url si book_id)
      - Construye secciones/bloques y persiste en DB
      - Inicia traducción async
    """
    if request.method == "GET":
        query = (request.GET.get("q") or "").strip()
        return render(request, "reader/upload.html", _library_context(request.user, query=query))

    # POST
    uploaded = request.FILES.get("epub_file")
    notify_email = (request.POST.get("notify_email") or "").strip()
    log_event(
        logger,
        logging.INFO,
        "book.upload.requested",
        user_id=request.user.id,
        has_file=bool(uploaded),
        notify_email=bool(notify_email),
    )

    if not uploaded:
        return render(
            request,
            "reader/upload.html",
            _library_context(request.user, error="No se recibió ningún archivo. Selecciona un .epub."),
        )

    result = validate_epub_file(uploaded)
    if not result:
        return render(
            request,
            "reader/upload.html",
            _library_context(request.user, error="Validación falló (sin detalle)."),
        )

    ok, err = result
    if not ok:
        return render(
            request,
            "reader/upload.html",
            _library_context(request.user, error=err or "El archivo no es un EPUB válido."),
        )

    if notify_email:
        try:
            validate_email(notify_email)
        except ValidationError:
            return render(
                request,
                "reader/upload.html",
                _library_context(request.user, error="El correo electrónico no es válido."),
            )

    # Carpeta del libro (estado + epub)
    tmp_uuid = uuid.uuid4()
    book_dir = Path(settings.MEDIA_ROOT) / "epub_books" / str(tmp_uuid)
    book_dir.mkdir(parents=True, exist_ok=True)

    local_epub_path = book_dir / "book.epub"

    # Guardado en disco (streaming, no en memoria)
    with open(local_epub_path, "wb") as out:
        for chunk in uploaded.chunks():
            out.write(chunk)

    # Extraer metadata + assets (incluye cover si book_id)
    info = extract_epub_info_from_path(str(local_epub_path), book_id=str(tmp_uuid))
    title = info.get("title") or "Libro"
    authors = ", ".join(info.get("authors") or [])
    description_html = info.get("description_html") or ""
    cover_url = info.get("cover_url") or ""

    # Construir secciones para el lector
    sections = build_reader_sections_with_blocks_from_spine(
        local_epub_path=str(local_epub_path),
        assets_root_url=f"{settings.MEDIA_URL}epub_assets/{tmp_uuid}/",
        max_section_chars=12000,
    )

    total_blocks = sum(len(sec.get("blocks", [])) for sec in sections)

    with transaction.atomic():
        book = Book.objects.create(
            id=tmp_uuid,
            owner=request.user,
            title=title,
            authors=authors,
            description_html=description_html,
            info=info,
            cover_url=cover_url,
            epub_path=f"epub_books/{tmp_uuid}/book.epub",
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

    start_book_translation_async(str(book.id))
    log_event(
        logger,
        logging.INFO,
        "book.upload.accepted",
        user_id=request.user.id,
        book_id=str(book.id),
        total_blocks=total_blocks,
    )
    return redirect("upload_epub")


@login_required
@require_GET
def read_book(request, book_id: str):
    """
    Vista principal del lector.
    """
    book = Book.objects.filter(pk=book_id, owner=request.user).first()
    if not book:
        raise Http404("Libro no encontrado. Vuelve a subir el EPUB.")
    if book.status != Book.Status.READY:
        raise Http404("El libro aún está en traducción. Vuelve más tarde.")
    log_event(
        logger,
        logging.INFO,
        "book.read.open",
        user_id=request.user.id,
        book_id=book_id,
    )

    progress = ReadingProgress.objects.filter(book=book, user=request.user).first()
    bookmarks = (
        Bookmark.objects.filter(book=book, user=request.user)
        .select_related("block")
        .order_by("-created_at")
    )

    sections_out = []
    sections = (
        Section.objects.filter(book=book)
        .prefetch_related("blocks")
        .order_by("index")
    )
    for section in sections:
        blocks_out = []
        blocks_list = list(section.blocks.all().order_by("index"))
        for block in blocks_list:
            translated = block.translated_html or block.original_html
            blocks_out.append({
                "id": block.id,
                "original_html": block.original_html,
                "translated_html": translated,
            })
        fallback_title = "Sección %d" % (section.index + 1)
        sections_out.append({
            "blocks": blocks_out,
            "title": _extract_section_title([b.original_html for b in blocks_list], fallback=fallback_title),
        })

    toc_entries = [
        {
            "section_index": idx,
            "title": sec.get("title") or f"Sección {idx + 1}",
        }
        for idx, sec in enumerate(sections_out)
    ]

    return render(request, "reader/read.html", {
        "book_id": book_id,
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
        "translation_enabled": True,
        "saved_progress": {
            "section_index": progress.section_index if progress else 0,
            "block_index": progress.block_index if progress else 0,
            "block_offset_percent": progress.block_offset_percent if progress else 0,
            "block_id": progress.block_id if progress else 0,
        },
    })


@require_GET
@login_required
def translate_block(request, book_id: str, section_idx: int, block_idx: int):
    """
    Traducción lazy por bloque.
    Nunca rompe la lectura:
      - Si índices fuera de rango o falla Ollama -> retorna HTML original.
    """
    book = Book.objects.filter(pk=book_id, owner=request.user).first()
    if not book:
        return JsonResponse({"ok": True, "translated_html": "<p></p>", "fallback": "missing_book"}, status=200)

    section = Section.objects.filter(book=book, index=section_idx).first()
    if not section:
        return JsonResponse({"ok": True, "translated_html": "<p></p>", "fallback": "section_oob"}, status=200)

    block = Block.objects.filter(section=section, index=block_idx).first()
    if not block:
        return JsonResponse({"ok": True, "translated_html": "<p></p>", "fallback": "block_oob"}, status=200)

    original_html = block.original_html or "<p></p>"
    if block.translated_html:
        log_event(
            logger,
            logging.INFO,
            "book.block.translate.cache_hit",
            user_id=request.user.id,
            book_id=book_id,
            section_idx=section_idx,
            block_idx=block_idx,
        )
        return JsonResponse({"ok": True, "translated_html": block.translated_html}, status=200)

    try:
        translated = translate_html_with_ollama(original_html)
        if not translated or not translated.strip():
            translated = original_html
        block.translated_html = translated
        block.save(update_fields=["translated_html"])
        log_event(
            logger,
            logging.INFO,
            "book.block.translate.generated",
            user_id=request.user.id,
            book_id=book_id,
            section_idx=section_idx,
            block_idx=block_idx,
        )
        return JsonResponse({"ok": True, "translated_html": translated}, status=200)
    except Exception:
        log_event(
            logger,
            logging.WARNING,
            "book.block.translate.fallback",
            user_id=request.user.id,
            book_id=book_id,
            section_idx=section_idx,
            block_idx=block_idx,
        )
        return JsonResponse({"ok": True, "translated_html": original_html, "fallback": "translate_failed"}, status=200)


@login_required
@require_POST
def save_progress(request, book_id: str):
    book = Book.objects.filter(pk=book_id, owner=request.user).first()
    if not book:
        return JsonResponse({"ok": False, "error": "missing_book"}, status=404)

    try:
        section_idx = int(request.POST.get("section_idx", "0"))
        block_idx = int(request.POST.get("block_idx", "0"))
        progress_percent = int(request.POST.get("progress_percent", "0"))
        block_offset_percent = float(request.POST.get("block_offset_percent", "0"))
        block_id = int(request.POST.get("block_id", "0"))
    except ValueError:
        return JsonResponse({"ok": False, "error": "invalid_payload"}, status=400)

    block_obj = None
    if block_id > 0:
        block_obj = Block.objects.filter(pk=block_id, section__book=book).first()
        if block_obj:
            section_idx = block_obj.section.index
            block_idx = block_obj.index

    ReadingProgress.objects.update_or_create(
        book=book,
        user=request.user,
        defaults={
            "block": block_obj,
            "section_index": max(0, section_idx),
            "block_index": max(0, block_idx),
            "block_offset_percent": max(0.0, min(1.0, block_offset_percent)),
            "progress_percent": max(0, min(100, progress_percent)),
        },
    )
    log_event(
        logger,
        logging.INFO,
        "book.progress.saved",
        user_id=request.user.id,
        book_id=book_id,
        section_idx=section_idx,
        block_idx=block_idx,
        progress_percent=progress_percent,
    )
    return JsonResponse({"ok": True}, status=200)


@require_POST
@login_required
def clear_book(request, book_id: str):
    """
    Borra en disco:
      - media/epub_books/<book_id>/
      - media/epub_assets/<book_id>/
    y elimina registros en base de datos.
    """
    book = Book.objects.filter(pk=book_id, owner=request.user).first()
    if not book:
        raise Http404("Libro no encontrado.")

    book_dir = Path(settings.MEDIA_ROOT) / "epub_books" / book_id
    assets_dir = Path(settings.MEDIA_ROOT) / "epub_assets" / book_id

    if book_dir.exists():
        shutil.rmtree(book_dir, ignore_errors=True)
    if assets_dir.exists():
        shutil.rmtree(assets_dir, ignore_errors=True)

    book.delete()
    log_event(
        logger,
        logging.INFO,
        "book.cleared",
        user_id=request.user.id,
        book_id=book_id,
    )

    return redirect("upload_epub")


@require_GET
@login_required
def list_bookmarks(request, book_id: str):
    book = Book.objects.filter(pk=book_id, owner=request.user).first()
    if not book:
        return JsonResponse({"ok": False, "error": "missing_book"}, status=404)

    bookmarks = Bookmark.objects.filter(book=book, user=request.user).order_by("-created_at")
    payload = [
        {
            "id": bm.id,
            "block_id": bm.block_id,
            "section_index": bm.section_index,
            "block_index": bm.block_index,
            "label": bm.label or f"Sección {bm.section_index + 1}, bloque {bm.block_index + 1}",
            "created_at": bm.created_at.isoformat(),
        }
        for bm in bookmarks
    ]
    return JsonResponse({"ok": True, "bookmarks": payload}, status=200)


@require_POST
@login_required
def create_bookmark(request, book_id: str):
    book = Book.objects.filter(pk=book_id, owner=request.user).first()
    if not book:
        return JsonResponse({"ok": False, "error": "missing_book"}, status=404)

    try:
        block_id = int(request.POST.get("block_id", "0"))
    except ValueError:
        return JsonResponse({"ok": False, "error": "invalid_payload"}, status=400)

    block = Block.objects.filter(pk=block_id, section__book=book).select_related("section").first()
    if not block:
        return JsonResponse({"ok": False, "error": "missing_block"}, status=404)

    label = (request.POST.get("label") or "").strip()[:200]
    bm, _created = Bookmark.objects.get_or_create(
        book=book,
        user=request.user,
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

    return JsonResponse({
        "ok": True,
        "bookmark": {
            "id": bm.id,
            "block_id": bm.block_id,
            "section_index": bm.section_index,
            "block_index": bm.block_index,
            "label": bm.label or f"Sección {bm.section_index + 1}, bloque {bm.block_index + 1}",
        },
    }, status=200)


@require_POST
@login_required
def delete_bookmark(request, book_id: str, bookmark_id: int):
    book = Book.objects.filter(pk=book_id, owner=request.user).first()
    if not book:
        return JsonResponse({"ok": False, "error": "missing_book"}, status=404)

    deleted, _ = Bookmark.objects.filter(
        pk=bookmark_id,
        book=book,
        user=request.user,
    ).delete()
    if not deleted:
        return JsonResponse({"ok": False, "error": "missing_bookmark"}, status=404)

    return JsonResponse({"ok": True}, status=200)
