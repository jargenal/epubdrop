import logging

from django.http import Http404, JsonResponse
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import services
from .logging_utils import log_event
from .tasks import start_book_translation_async

logger = logging.getLogger(__name__)


@require_GET
@login_required
def translation_progress(request):
    payload = services.build_translation_progress_payload(request.user)
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
    payload = services.build_metrics_summary_payload(request.user)
    log_event(
        logger,
        logging.INFO,
        "library.metrics.summary",
        user_id=request.user.id,
        payload=payload,
    )
    return JsonResponse({"ok": True, "metrics": payload}, status=200)


@require_http_methods(["GET", "POST"])
@login_required
def upload_epub(request):
    """
    GET:
      - Biblioteca con búsqueda y sección de carga

    POST:
      - Valida EPUB
      - Guarda a disco (streaming)
      - Extrae assets + metadata
      - Construye secciones/bloques y persiste en DB
    """
    if request.method == "GET":
        query = (request.GET.get("q") or "").strip()
        return render(request, "reader/upload.html", services.build_library_context(request.user, query=query))

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

    book, total_blocks, error = services.create_book_from_upload(request.user, uploaded, notify_email)
    if error:
        return render(
            request,
            "reader/upload.html",
            services.build_library_context(request.user, error=error),
        )

    log_event(
        logger,
        logging.INFO,
        "book.upload.accepted",
        user_id=request.user.id,
        book_id=str(book.id),
        total_blocks=total_blocks,
        translation_mode="background_with_lazy_fallback",
    )
    start_book_translation_async(str(book.id))
    return redirect("upload_epub")


@login_required
@require_GET
def read_book(request, book_id: str):
    book = services.get_owned_book(request.user, book_id)
    if not book:
        raise Http404("Libro no encontrado. Vuelve a subir el EPUB.")

    log_event(
        logger,
        logging.INFO,
        "book.read.open",
        user_id=request.user.id,
        book_id=book_id,
        status=book.status,
    )
    return render(request, "reader/read.html", services.build_reader_context(book, request.user))


@require_GET
@login_required
def translate_block(request, book_id: str, section_idx: int, block_idx: int):
    payload = services.translate_block_for_user(request.user, book_id, section_idx, block_idx)
    event = payload.pop("_log_event", "")
    if event:
        level = logging.WARNING if payload.get("fallback") == "translate_failed" else logging.INFO
        log_kwargs = {
            "user_id": request.user.id,
            "book_id": book_id,
            "section_idx": section_idx,
            "block_idx": block_idx,
        }
        if "saved" in payload:
            log_kwargs["saved"] = payload.get("saved")
        log_event(
            logger,
            level,
            event,
            **log_kwargs,
        )
    return JsonResponse(payload, status=200)


@login_required
@require_POST
def save_progress(request, book_id: str):
    payload, status, log_payload = services.save_progress_for_user(request.user, book_id, request.POST)
    if log_payload:
        log_event(
            logger,
            logging.INFO,
            "book.progress.saved",
            user_id=request.user.id,
            book_id=book_id,
            **log_payload,
        )
    return JsonResponse(payload, status=status)


@require_POST
@login_required
def clear_book(request, book_id: str):
    if not services.clear_book_for_user(request.user, book_id):
        raise Http404("Libro no encontrado.")

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
    payload, status = services.list_bookmarks_for_user(request.user, book_id)
    return JsonResponse(payload, status=status)


@require_POST
@login_required
def create_bookmark(request, book_id: str):
    payload, status = services.create_bookmark_for_user(request.user, book_id, request.POST)
    return JsonResponse(payload, status=status)


@require_POST
@login_required
def delete_bookmark(request, book_id: str, bookmark_id: int):
    payload, status = services.delete_bookmark_for_user(request.user, book_id, bookmark_id)
    return JsonResponse(payload, status=status)
