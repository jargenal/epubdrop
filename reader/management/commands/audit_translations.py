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
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Solo reporta bloques inválidos; no guarda reparaciones.",
        )
        parser.add_argument(
            "--no-fallback-original",
            action="store_true",
            help="Si la retraducción falla, conserva la traducción existente en lugar de guardar el original.",
        )
        parser.add_argument("--section-index", type=int, help="Limita la auditoría a una sección concreta.")
        parser.add_argument("--block-index", type=int, help="Limita la auditoría a un bloque concreto.")
        parser.add_argument("--limit", type=int, help="Máximo de bloques inválidos a reparar o evaluar.")
        parser.add_argument(
            "--details",
            action="store_true",
            help="Muestra sección, bloque, razón y acción para cada bloque inválido.",
        )

    def handle(self, *args, **options):
        book_id = options.get("book_id")
        all_ready = bool(options.get("all_ready"))
        dry_run = bool(options.get("dry_run"))
        fallback_to_original = not bool(options.get("no_fallback_original"))
        section_index = options.get("section_index")
        block_index = options.get("block_index")
        limit = options.get("limit")
        details = bool(options.get("details"))

        if not book_id and not all_ready:
            raise CommandError("Debes indicar --book-id <uuid> o --all-ready")
        if limit is not None and limit < 1:
            raise CommandError("--limit debe ser mayor que 0")
        if all_ready and (section_index is not None or block_index is not None):
            raise CommandError("--section-index y --block-index solo se permiten con --book-id")

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
            "invalid": 0,
            "repaired": 0,
            "fallback_original": 0,
            "skipped_unrepaired": 0,
        }
        for book in books:
            stats = sanitize_book_translations(
                str(book.id),
                force_refresh=True,
                dry_run=dry_run,
                fallback_to_original=fallback_to_original,
                section_index=section_index,
                block_index=block_index,
                limit=limit,
            )
            total["books"] += 1
            total["scanned"] += stats.get("scanned", 0)
            total["valid"] += stats.get("valid", 0)
            total["invalid"] += stats.get("invalid", 0)
            total["repaired"] += stats.get("repaired", 0)
            total["fallback_original"] += stats.get("fallback_original", 0)
            total["skipped_unrepaired"] += stats.get("skipped_unrepaired", 0)
            self.stdout.write(
                self.style.SUCCESS(
                    f"[{book.id}] scanned={stats.get('scanned', 0)} "
                    f"valid={stats.get('valid', 0)} invalid={stats.get('invalid', 0)} "
                    f"repaired={stats.get('repaired', 0)} "
                    f"fallback_original={stats.get('fallback_original', 0)} "
                    f"skipped_unrepaired={stats.get('skipped_unrepaired', 0)}"
                )
            )
            invalid_reasons = stats.get("invalid_reasons", {})
            if invalid_reasons:
                reason_summary = ", ".join(f"{reason}={count}" for reason, count in sorted(invalid_reasons.items()))
                self.stdout.write(f"  reasons: {reason_summary}")
            if details:
                for block in stats.get("blocks", []):
                    extra = ""
                    if block.get("repair_failed_reason"):
                        extra = f" repair_failed_reason={block['repair_failed_reason']}"
                    self.stdout.write(
                        "  "
                        f"section={block['section_index']} block={block['block_index']} "
                        f"reason={block['reason']} action={block.get('action', 'none')}"
                        f"{extra}"
                    )

        self.stdout.write(
            self.style.WARNING(
                "TOTAL "
                f"books={total['books']} scanned={total['scanned']} "
                f"valid={total['valid']} invalid={total['invalid']} "
                f"repaired={total['repaired']} fallback_original={total['fallback_original']} "
                f"skipped_unrepaired={total['skipped_unrepaired']}"
            )
        )
