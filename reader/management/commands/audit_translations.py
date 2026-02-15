from django.core.management.base import BaseCommand, CommandError

from reader.models import Book
from reader.utils import sanitize_book_translations


class Command(BaseCommand):
    help = "Audita y repara traducciones inválidas en bloques ya procesados."

    def add_arguments(self, parser):
        parser.add_argument("--book-id", type=str, help="UUID de libro a auditar")
        parser.add_argument(
            "--all-ready",
            action="store_true",
            help="Audita todos los libros en estado READY",
        )

    def handle(self, *args, **options):
        book_id = options.get("book_id")
        all_ready = bool(options.get("all_ready"))

        if not book_id and not all_ready:
            raise CommandError("Debes indicar --book-id <uuid> o --all-ready")

        books = []
        if book_id:
            book = Book.objects.filter(pk=book_id).first()
            if not book:
                raise CommandError(f"Libro no encontrado: {book_id}")
            books = [book]
        elif all_ready:
            books = list(Book.objects.filter(status=Book.Status.READY).order_by("-updated_at"))

        total = {
            "books": 0,
            "scanned": 0,
            "valid": 0,
            "repaired": 0,
            "fallback_original": 0,
        }
        for book in books:
            stats = sanitize_book_translations(str(book.id), force_refresh=True)
            total["books"] += 1
            total["scanned"] += stats.get("scanned", 0)
            total["valid"] += stats.get("valid", 0)
            total["repaired"] += stats.get("repaired", 0)
            total["fallback_original"] += stats.get("fallback_original", 0)
            self.stdout.write(
                self.style.SUCCESS(
                    f"[{book.id}] scanned={stats.get('scanned', 0)} "
                    f"valid={stats.get('valid', 0)} repaired={stats.get('repaired', 0)} "
                    f"fallback_original={stats.get('fallback_original', 0)}"
                )
            )

        self.stdout.write(
            self.style.WARNING(
                "TOTAL "
                f"books={total['books']} scanned={total['scanned']} "
                f"valid={total['valid']} repaired={total['repaired']} "
                f"fallback_original={total['fallback_original']}"
            )
        )
