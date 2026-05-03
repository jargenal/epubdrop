from django.core.management.base import BaseCommand, CommandError

from reader.models import Book
from reader.tasks import _translate_book, prepare_book_for_translation


class Command(BaseCommand):
    help = "Reinicia la traducción completa de un libro usando el código actual."

    def add_arguments(self, parser):
        parser.add_argument("--book-id", type=str, required=True, help="UUID del libro a reiniciar")

    def handle(self, *args, **options):
        book_id = options["book_id"]
        book = Book.objects.filter(pk=book_id).first()
        if not book:
            raise CommandError(f"Libro no encontrado: {book_id}")

        run_id = prepare_book_for_translation(book_id, reset_blocks=True)
        if not run_id:
            raise CommandError(f"No se pudo preparar la traducción para: {book_id}")

        self.stdout.write(self.style.WARNING(f"[{book_id}] reinicio iniciado con run_id={run_id}"))
        _translate_book(book_id, run_id=run_id)

        book.refresh_from_db()
        self.stdout.write(
            self.style.SUCCESS(
                f"[{book_id}] status={book.status} translated_blocks={book.translated_blocks}/{book.total_blocks}"
            )
        )
