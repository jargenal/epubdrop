from django.urls import path
from . import views

urlpatterns = [
    path("", views.upload_epub, name="upload_epub"),
    path("read/<str:book_id>/", views.read_book, name="read_book"),

    # API: traducción lazy por bloque (book-specific)
    path(
        "api/books/<str:book_id>/translate-block/<int:section_idx>/<int:block_idx>/",
        views.translate_block,
        name="translate_block",
    ),
    path(
        "api/books/<str:book_id>/progress/",
        views.save_progress,
        name="save_progress",
    ),

    # Limpiar (borra en disco) - POST
    path("clear/<str:book_id>/", views.clear_book, name="clear_book"),
]
