import threading
import logging
import time
import uuid
from typing import Optional

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import Book, Section, Block
from .logging_utils import log_event
from .utils import is_valid_translation_html, translate_html_with_ollama

logger = logging.getLogger(__name__)
TRANSLATION_RUN_ID_KEY = "translation_run_id"
TRANSLATION_RUN_CHECK_EVERY_BLOCKS = 8
TRANSLATION_RUN_CHECK_MAX_INTERVAL_SECONDS = 1.0


def prepare_book_for_translation(book_id: str, *, reset_blocks: bool = False) -> Optional[str]:
    book = Book.objects.filter(pk=book_id).first()
    if not book:
        return None

    run_id = uuid.uuid4().hex
    info = dict(book.info or {})
    info[TRANSLATION_RUN_ID_KEY] = run_id
    info.pop("translation_disabled", None)

    with transaction.atomic():
        if reset_blocks:
            Block.objects.filter(section__book_id=book_id).update(translated_html="")
        Book.objects.filter(pk=book_id).update(
            status=Book.Status.TRANSLATING,
            error_message="",
            translated_blocks=0,
            info=info,
            updated_at=timezone.now(),
        )
    return run_id


def _count_translated_blocks(book_id: str) -> int:
    return Block.objects.filter(
        section__book_id=book_id,
    ).exclude(translated_html="").count()


def _sync_book_translation_progress(book_id: str, *, complete_if_ready: bool = False) -> int:
    translated_count = _count_translated_blocks(book_id)
    update_fields = {
        "translated_blocks": translated_count,
        "updated_at": timezone.now(),
    }
    if complete_if_ready:
        total_blocks = Book.objects.filter(pk=book_id).values_list("total_blocks", flat=True).first() or 0
        if total_blocks > 0 and translated_count >= total_blocks:
            update_fields["status"] = Book.Status.READY
            update_fields["error_message"] = ""
    Book.objects.filter(pk=book_id).update(**update_fields)
    return translated_count


def _translation_run_is_current(book_id: str, run_id: str) -> bool:
    info = Book.objects.filter(pk=book_id).values_list("info", flat=True).first() or {}
    if not isinstance(info, dict):
        return False
    return str(info.get(TRANSLATION_RUN_ID_KEY) or "") == str(run_id or "")


class _TranslationRunGuard:
    def __init__(
        self,
        book_id: str,
        run_id: str,
        *,
        check_every_blocks: int = TRANSLATION_RUN_CHECK_EVERY_BLOCKS,
        max_interval_seconds: float = TRANSLATION_RUN_CHECK_MAX_INTERVAL_SECONDS,
    ) -> None:
        self.book_id = book_id
        self.run_id = run_id
        self.check_every_blocks = max(1, int(check_every_blocks))
        self.max_interval_seconds = max(0.0, float(max_interval_seconds))
        self._blocks_since_check = self.check_every_blocks
        self._last_check_at = 0.0
        self._is_current = True

    def is_current(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        should_refresh = force or self._blocks_since_check >= self.check_every_blocks
        if not should_refresh and self.max_interval_seconds == 0:
            should_refresh = True
        if not should_refresh and (now - self._last_check_at) >= self.max_interval_seconds:
            should_refresh = True
        if should_refresh:
            self._is_current = _translation_run_is_current(self.book_id, self.run_id)
            self._last_check_at = now
            self._blocks_since_check = 0
        return self._is_current

    def mark_block_processed(self) -> None:
        self._blocks_since_check += 1


def _log_translation_superseded(book_id: str, run_id: str, *, section_index: Optional[int] = None, block_index: Optional[int] = None) -> None:
    payload = {
        "book_id": book_id,
        "run_id": run_id,
    }
    if section_index is not None:
        payload["section_index"] = section_index
    if block_index is not None:
        payload["block_index"] = block_index
    log_event(logger, logging.INFO, "translation.book.superseded", **payload)


def start_book_translation_async(book_id: str, *, run_id: Optional[str] = None) -> None:
    if not run_id:
        run_id = prepare_book_for_translation(book_id, reset_blocks=False)
        if not run_id:
            log_event(logger, logging.WARNING, "translation.thread.missing_book", book_id=book_id)
            return

    log_event(logger, logging.INFO, "translation.thread.start", book_id=book_id, run_id=run_id)
    thread = threading.Thread(
        target=_translate_book,
        args=(book_id, run_id),
        daemon=True,
    )
    thread.start()


def _translate_book(book_id: str, run_id: Optional[str] = None) -> None:
    try:
        book = Book.objects.get(pk=book_id)
    except Book.DoesNotExist:
        log_event(logger, logging.WARNING, "translation.book.missing", book_id=book_id)
        return

    if not run_id:
        info = dict(book.info or {})
        run_id = str(info.get(TRANSLATION_RUN_ID_KEY) or "")
        if not run_id:
            run_id = prepare_book_for_translation(book_id, reset_blocks=False)
            if not run_id:
                log_event(logger, logging.WARNING, "translation.book.prepare_failed", book_id=book_id)
                return
            book.refresh_from_db()

    run_guard = _TranslationRunGuard(book_id, run_id)

    if not run_guard.is_current(force=True):
        _log_translation_superseded(book_id, run_id)
        return

    _sync_book_translation_progress(book_id)
    log_event(logger, logging.INFO, "translation.book.begin", book_id=book_id, title=book.title, run_id=run_id)

    had_errors = False

    try:
        sections = Section.objects.filter(book=book).prefetch_related("blocks").order_by("index")
        for section in sections:
            for block in section.blocks.all().order_by("index"):
                if not run_guard.is_current():
                    _log_translation_superseded(
                        book_id,
                        run_id,
                        section_index=section.index,
                        block_index=block.index,
                    )
                    return
                current_translated_html = Block.objects.filter(pk=block.pk).values_list("translated_html", flat=True).first()
                if current_translated_html:
                    run_guard.mark_block_processed()
                    continue

                try:
                    translated_html = translate_html_with_ollama(block.original_html)
                    if not translated_html.strip() or not is_valid_translation_html(block.original_html, translated_html):
                        had_errors = True
                        log_event(
                            logger,
                            logging.WARNING,
                            "translation.block.invalid_fallback",
                            book_id=book_id,
                            section_index=section.index,
                            block_index=block.index,
                        )
                        run_guard.mark_block_processed()
                        continue
                except Exception:
                    had_errors = True
                    log_event(
                        logger,
                        logging.WARNING,
                        "translation.block.fallback",
                        book_id=book_id,
                        section_index=section.index,
                        block_index=block.index,
                    )
                    run_guard.mark_block_processed()
                    continue

                if not run_guard.is_current():
                    _log_translation_superseded(
                        book_id,
                        run_id,
                        section_index=section.index,
                        block_index=block.index,
                    )
                    return

                updated = Block.objects.filter(pk=block.pk, translated_html="").update(translated_html=translated_html)
                if updated:
                    _sync_book_translation_progress(book_id)
                run_guard.mark_block_processed()

        if not run_guard.is_current(force=True):
            _log_translation_superseded(book_id, run_id)
            return

        translated_count = _sync_book_translation_progress(book_id, complete_if_ready=True)
        final_book = Book.objects.filter(pk=book_id).only("status", "total_blocks").first()
        if final_book and final_book.status != Book.Status.READY:
            Book.objects.filter(pk=book_id).update(
                status=Book.Status.TRANSLATING,
                error_message="Algunos bloques no obtuvieron traducción válida; se reintentarán bajo demanda.",
                updated_at=timezone.now(),
            )
        log_event(
            logger,
            logging.INFO,
            "translation.book.completed",
            book_id=book_id,
            run_id=run_id,
            had_errors=had_errors,
            translated_blocks=translated_count,
        )
        _send_completion_email(book, had_errors=had_errors)
    except Exception as exc:
        if run_id and not run_guard.is_current(force=True):
            _log_translation_superseded(book_id, run_id)
            return
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
