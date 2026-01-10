import shutil
import uuid
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .utils import (
    validate_epub_file,
    extract_epub_info_from_path,
    build_reader_sections_with_blocks_from_spine,
    save_book_state,
    load_book_state,
    translate_html_with_libretranslate,
    get_original_block_html,
)


@require_http_methods(["GET", "POST"])
def upload_epub(request):
    """
    GET:
      - Muestra pantalla de upload con Tailwind
      - Habilita Leer/Limpiar si existe last_book_id

    POST:
      - Valida EPUB
      - Guarda a disco (streaming)
      - Extrae assets + metadata (incluye cover_url si book_id)
      - Construye secciones/bloques
      - Guarda estado en JSON por book_id
      - Guarda last_book_id en session
      - Redirige a read_book
    """
    if request.method == "GET":
        last_book_id = (request.session.get("last_book_id") or "").strip() or None
        context = {
            "has_last_upload": bool(last_book_id),
            "last_book_id": last_book_id,
            "error": None,
            "info": None,
        }
        return render(request, "reader/upload.html", context)

    # POST
    uploaded = request.FILES.get("epub_file")
    last_book_id = (request.session.get("last_book_id") or "").strip() or None

    if not uploaded:
        return render(request, "reader/upload.html", {
            "has_last_upload": bool(last_book_id),
            "last_book_id": last_book_id,
            "error": "No se recibió ningún archivo. Selecciona un .epub.",
            "info": None,
        })

    result = validate_epub_file(uploaded)
    if not result:
        return render(request, "reader/upload.html", {
            "has_last_upload": bool(last_book_id),
            "last_book_id": last_book_id,
            "error": "Validación falló (sin detalle).",
            "info": None,
        })

    ok, err = result
    if not ok:
        return render(request, "reader/upload.html", {
            "has_last_upload": bool(last_book_id),
            "last_book_id": last_book_id,
            "error": err or "El archivo no es un EPUB válido.",
            "info": None,
        })

    # Nuevo id para este libro
    book_id = uuid.uuid4().hex

    # Carpeta del libro (estado + epub)
    book_dir = Path(settings.MEDIA_ROOT) / "epub_books" / book_id
    book_dir.mkdir(parents=True, exist_ok=True)

    local_epub_path = book_dir / "book.epub"

    # Guardado en disco (streaming, no en memoria)
    with open(local_epub_path, "wb") as out:
        for chunk in uploaded.chunks():
            out.write(chunk)

    # Extraer metadata + assets (incluye cover si book_id)
    info = extract_epub_info_from_path(str(local_epub_path), book_id=book_id)
    title = info.get("title") or "Libro"

    # Construir secciones para el lector
    assets_base_url = f"{settings.MEDIA_URL}epub_assets/{book_id}/"
    sections = build_reader_sections_with_blocks_from_spine(
        local_epub_path=str(local_epub_path),
        assets_root_url=assets_base_url,
        max_section_chars=12000,
    )

    # Guardar estado del libro (JSON)
    save_book_state(book_id, {
        "title": title,
        "info": info,
        "sections": sections,
    })

    # Guardar último libro para habilitar "Leer" en upload
    request.session["last_book_id"] = book_id

    return redirect("read_book", book_id=book_id)


@require_GET
def read_book(request, book_id: str):
    """
    Vista principal del lector.
    """
    state = load_book_state(book_id)
    if not state:
        raise Http404("Libro no encontrado o expirado. Vuelve a subir el EPUB.")

    return render(request, "reader/read.html", {
        "book_id": book_id,
        "title": state.get("title", "Libro"),
        "info": state.get("info", {}),
        "sections": state.get("sections", []),
        "translation_enabled": True,
    })


@require_GET
def translate_block(request, book_id: str, section_idx: int, block_idx: int):
    """
    Traducción lazy por bloque.
    Nunca rompe la lectura:
      - Si índices fuera de rango o falla LibreTranslate -> retorna HTML original.
    """
    state = load_book_state(book_id)
    if not state:
        return JsonResponse({"ok": True, "translated_html": "<p></p>", "fallback": "missing_book"}, status=200)

    sections = state.get("sections", []) or []

    # Fuera de rango => devolvemos original si se puede
    if section_idx < 0 or section_idx >= len(sections):
        original = get_original_block_html(sections, section_idx, block_idx) or "<p></p>"
        return JsonResponse({"ok": True, "translated_html": original, "fallback": "section_oob"}, status=200)

    blocks = sections[section_idx].get("blocks", []) or []
    if block_idx < 0 or block_idx >= len(blocks):
        original = get_original_block_html(sections, section_idx, block_idx) or "<p></p>"
        return JsonResponse({"ok": True, "translated_html": original, "fallback": "block_oob"}, status=200)

    original_html = blocks[block_idx] or "<p></p>"

    try:
        translated = translate_html_with_libretranslate(original_html)
        if not translated or not translated.strip():
            translated = original_html
        return JsonResponse({"ok": True, "translated_html": translated}, status=200)
    except Exception:
        return JsonResponse({"ok": True, "translated_html": original_html, "fallback": "translate_failed"}, status=200)


@require_POST
def clear_book(request, book_id: str):
    """
    Borra en disco:
      - media/epub_books/<book_id>/
      - media/epub_assets/<book_id>/
    y limpia la session si era el último libro.
    """
    book_dir = Path(settings.MEDIA_ROOT) / "epub_books" / book_id
    assets_dir = Path(settings.MEDIA_ROOT) / "epub_assets" / book_id

    if book_dir.exists():
        shutil.rmtree(book_dir, ignore_errors=True)
    if assets_dir.exists():
        shutil.rmtree(assets_dir, ignore_errors=True)

    # Limpia last_book_id si coincide
    last_book_id = (request.session.get("last_book_id") or "").strip()
    if last_book_id == book_id:
        request.session.pop("last_book_id", None)

    return redirect("upload_epub")