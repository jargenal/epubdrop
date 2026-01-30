import shutil
import uuid
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from lxml import html as lxml_html

from .models import Book, Section, Block, ReadingProgress
from .tasks import start_book_translation_async
from .utils import (
    validate_epub_file,
    extract_epub_info_from_path,
    build_reader_sections_with_blocks_from_spine,
    translate_html_with_ollama,
)


def _library_context(query: str = "", error: Optional[str] = None, info: Optional[str] = None) -> dict:
    books = Book.objects.all().order_by("-created_at")
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
def translation_progress(request):
    books = Book.objects.all().order_by("-created_at")
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
    return JsonResponse({"in_progress": payload})


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
        return render(request, "reader/upload.html", _library_context(query=query))

    # POST
    uploaded = request.FILES.get("epub_file")
    notify_email = (request.POST.get("notify_email") or "").strip()

    if not uploaded:
        return render(
            request,
            "reader/upload.html",
            _library_context(error="No se recibió ningún archivo. Selecciona un .epub."),
        )

    result = validate_epub_file(uploaded)
    if not result:
        return render(
            request,
            "reader/upload.html",
            _library_context(error="Validación falló (sin detalle)."),
        )

    ok, err = result
    if not ok:
        return render(
            request,
            "reader/upload.html",
            _library_context(error=err or "El archivo no es un EPUB válido."),
        )

    if notify_email:
        try:
            validate_email(notify_email)
        except ValidationError:
            return render(
                request,
                "reader/upload.html",
                _library_context(error="El correo electrónico no es válido."),
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
    return redirect("upload_epub")


@login_required
@require_GET
def read_book(request, book_id: str):
    """
    Vista principal del lector.
    """
    book = Book.objects.filter(pk=book_id).first()
    if not book:
        raise Http404("Libro no encontrado. Vuelve a subir el EPUB.")
    if book.status != Book.Status.READY:
        raise Http404("El libro aún está en traducción. Vuelve más tarde.")

    progress = ReadingProgress.objects.filter(book=book, user=request.user).first()

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

    return render(request, "reader/read.html", {
        "book_id": book_id,
        "title": book.title,
        "info": book.info or {},
        "sections": sections_out,
        "translation_enabled": True,
        "saved_progress": {
            "section_index": progress.section_index if progress else 0,
            "block_index": progress.block_index if progress else 0,
            "block_offset_percent": progress.block_offset_percent if progress else 0,
            "block_id": progress.block_id if progress else 0,
        },
    })


@require_GET
def translate_block(request, book_id: str, section_idx: int, block_idx: int):
    """
    Traducción lazy por bloque.
    Nunca rompe la lectura:
      - Si índices fuera de rango o falla Ollama -> retorna HTML original.
    """
    book = Book.objects.filter(pk=book_id).first()
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
        return JsonResponse({"ok": True, "translated_html": block.translated_html}, status=200)

    try:
        translated = translate_html_with_ollama(original_html)
        if not translated or not translated.strip():
            translated = original_html
        block.translated_html = translated
        block.save(update_fields=["translated_html"])
        return JsonResponse({"ok": True, "translated_html": translated}, status=200)
    except Exception:
        return JsonResponse({"ok": True, "translated_html": original_html, "fallback": "translate_failed"}, status=200)


@login_required
@require_POST
def save_progress(request, book_id: str):
    book = Book.objects.filter(pk=book_id).first()
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
    return JsonResponse({"ok": True}, status=200)


@require_POST
def clear_book(request, book_id: str):
    """
    Borra en disco:
      - media/epub_books/<book_id>/
      - media/epub_assets/<book_id>/
    y elimina registros en base de datos.
    """
    book_dir = Path(settings.MEDIA_ROOT) / "epub_books" / book_id
    assets_dir = Path(settings.MEDIA_ROOT) / "epub_assets" / book_id

    if book_dir.exists():
        shutil.rmtree(book_dir, ignore_errors=True)
    if assets_dir.exists():
        shutil.rmtree(assets_dir, ignore_errors=True)

    Book.objects.filter(pk=book_id).delete()

    return redirect("upload_epub")
