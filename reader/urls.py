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
    path(
        "api/books/<str:book_id>/bookmarks/",
        views.list_bookmarks,
        name="list_bookmarks",
    ),
    path(
        "api/books/<str:book_id>/bookmarks/create/",
        views.create_bookmark,
        name="create_bookmark",
    ),
    path(
        "api/books/<str:book_id>/bookmarks/<int:bookmark_id>/delete/",
        views.delete_bookmark,
        name="delete_bookmark",
    ),
    path("api/progress/", views.translation_progress, name="translation_progress"),
    path("api/metrics/", views.metrics_summary, name="metrics_summary"),

    # Limpiar (borra en disco) - POST
    path("clear/<str:book_id>/", views.clear_book, name="clear_book"),
]
