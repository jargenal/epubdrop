import threading
from typing import Optional

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import F
from django.utils import timezone

from .models import Book, Section, Block
from .utils import translate_html_with_ollama


def start_book_translation_async(book_id: str) -> None:
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
        return

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

                Block.objects.filter(pk=block.pk).update(translated_html=translated_html)
                Book.objects.filter(pk=book_id).update(
                    translated_blocks=F("translated_blocks") + 1,
                    updated_at=timezone.now(),
                )

        Book.objects.filter(pk=book_id).update(
            status=Book.Status.READY,
            error_message="",
            updated_at=timezone.now(),
        )
        _send_completion_email(book, had_errors=had_errors)
    except Exception as exc:
        Book.objects.filter(pk=book_id).update(
            status=Book.Status.FAILED,
            error_message=str(exc)[:500],
            updated_at=timezone.now(),
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
