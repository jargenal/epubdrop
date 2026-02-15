import threading
import logging
from typing import Optional

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import F
from django.utils import timezone

from .models import Book, Section, Block
from .logging_utils import log_event
from .utils import translate_html_with_ollama, sanitize_book_translations

logger = logging.getLogger(__name__)


def start_book_translation_async(book_id: str) -> None:
    log_event(logger, logging.INFO, "translation.thread.start", book_id=book_id)
    thread = threading.Thread(
        target=_translate_book,
        args=(book_id,),
        daemon=True,
    )
    thread.start()


def _translate_book(book_id: str) -> None:
    try:
        book = Book.objects.get(pk=book_id)
    except Book.DoesNotExist:
        log_event(logger, logging.WARNING, "translation.book.missing", book_id=book_id)
        return

    log_event(logger, logging.INFO, "translation.book.begin", book_id=book_id, title=book.title)

    Book.objects.filter(pk=book_id).update(
        status=Book.Status.TRANSLATING,
        error_message="",
        translated_blocks=0,
        updated_at=timezone.now(),
    )

    had_errors = False

    try:
        sections = Section.objects.filter(book=book).prefetch_related("blocks").order_by("index")
        for section in sections:
            for block in section.blocks.all().order_by("index"):
                if block.translated_html:
                    Book.objects.filter(pk=book_id).update(
                        translated_blocks=F("translated_blocks") + 1,
                        updated_at=timezone.now(),
                    )
                    continue

                translated_html = ""
                try:
                    translated_html = translate_html_with_ollama(block.original_html)
                    if not translated_html.strip():
                        translated_html = block.original_html
                except Exception:
                    translated_html = block.original_html
                    had_errors = True
                    log_event(
                        logger,
                        logging.WARNING,
                        "translation.block.fallback",
                        book_id=book_id,
                        section_index=section.index,
                        block_index=block.index,
                    )

                Block.objects.filter(pk=block.pk).update(translated_html=translated_html)
                Book.objects.filter(pk=book_id).update(
                    translated_blocks=F("translated_blocks") + 1,
                    updated_at=timezone.now(),
                )

        sanitize_stats = sanitize_book_translations(book_id, force_refresh=True)
        if sanitize_stats.get("repaired", 0) > 0 or sanitize_stats.get("fallback_original", 0) > 0:
            had_errors = True
        log_event(
            logger,
            logging.INFO,
            "translation.book.sanitize.completed",
            book_id=book_id,
            scanned=sanitize_stats.get("scanned", 0),
            valid=sanitize_stats.get("valid", 0),
            repaired=sanitize_stats.get("repaired", 0),
            fallback_original=sanitize_stats.get("fallback_original", 0),
        )

        Book.objects.filter(pk=book_id).update(
            status=Book.Status.READY,
            error_message="",
            updated_at=timezone.now(),
        )
        log_event(
            logger,
            logging.INFO,
            "translation.book.completed",
            book_id=book_id,
            had_errors=had_errors,
        )
        _send_completion_email(book, had_errors=had_errors)
    except Exception as exc:
        Book.objects.filter(pk=book_id).update(
            status=Book.Status.FAILED,
            error_message=str(exc)[:500],
            updated_at=timezone.now(),
        )
        log_event(
            logger,
            logging.ERROR,
            "translation.book.failed",
            book_id=book_id,
            error=str(exc)[:500],
        )
        _send_completion_email(book, failed=True)


def _send_completion_email(
    book: Book,
    had_errors: Optional[bool] = False,
    failed: Optional[bool] = False,
) -> None:
    try:
        book = Book.objects.get(pk=book.pk)
    except Book.DoesNotExist:
        return
    if not book.notify_email:
        return

    if failed:
        subject = f"No se pudo completar la traducción: {book.title}"
        message = (
            f"La traducción no pudo completarse para '{book.title}'.\n"
            "Revisa la configuración de Ollama o intenta nuevamente."
        )
    else:
        subject = f"Tu libro está listo: {book.title}"
        if had_errors:
            subject = f"Tu libro está listo (con advertencias): {book.title}"
        message = (
            f"La traducción ha finalizado para '{book.title}'.\n"
            "Ya puedes abrirlo en la biblioteca."
        )
    send_mail(
        subject=subject,
        message=message,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        recipient_list=[book.notify_email],
        fail_silently=True,
    )
