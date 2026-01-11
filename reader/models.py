import uuid
from typing import Optional

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models


class CustomUserManager(BaseUserManager):
    def create_user(self, email: str, password: "Optional[str]" = None, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: "Optional[str]" = None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True")
        return self.create_user(email, password, **extra_fields)


class CustomUser(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=150, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)

    objects = CustomUserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    def __str__(self) -> str:
        return self.email


class Book(models.Model):
    class Status(models.TextChoices):
        TRANSLATING = "translating", "Translating"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255, default="Libro")
    authors = models.CharField(max_length=512, blank=True)
    description_html = models.TextField(blank=True)
    info = models.JSONField(default=dict, blank=True)
    cover_url = models.CharField(max_length=512, blank=True)
    epub_path = models.CharField(max_length=512, blank=True)
    notify_email = models.EmailField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.TRANSLATING,
    )
    total_blocks = models.PositiveIntegerField(default=0)
    translated_blocks = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.title

    @property
    def progress_percent(self) -> int:
        if self.total_blocks <= 0:
            return 0
        return int((self.translated_blocks / self.total_blocks) * 100)


class Section(models.Model):
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="sections")
    index = models.PositiveIntegerField()

    class Meta:
        ordering = ["index"]

    def __str__(self) -> str:
        return f"{self.book_id}:{self.index}"


class Block(models.Model):
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="blocks")
    index = models.PositiveIntegerField()
    original_html = models.TextField()
    translated_html = models.TextField(blank=True)

    class Meta:
        ordering = ["index"]

    def __str__(self) -> str:
        return f"{self.section_id}:{self.index}"


class ReadingProgress(models.Model):
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="progress_entries")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reading_progress")
    block = models.ForeignKey("Block", on_delete=models.SET_NULL, null=True, blank=True, related_name="progress_entries")
    section_index = models.PositiveIntegerField(default=0)
    block_index = models.PositiveIntegerField(default=0)
    block_offset_percent = models.FloatField(default=0.0)
    progress_percent = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["book", "user"], name="uniq_progress_per_user_book"),
        ]

    def __str__(self) -> str:
        return f"{self.book_id}:{self.user_id}"
