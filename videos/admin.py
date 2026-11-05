"""Admin configuration for secure video catalog management."""

from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib import admin
from django.core.exceptions import ValidationError

from videos.models import Video

VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".webm"}
THUMBNAIL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def validate_upload(upload, allowed_extensions):
    """Reject empty, unsupported, or oversized admin uploads."""
    if not upload:
        return upload
    extension = Path(upload.name).suffix.lower()
    if extension not in allowed_extensions:
        raise ValidationError("This file type is not allowed.")
    if upload.size == 0 or upload.size > settings.VIDEO_MAX_UPLOAD_BYTES:
        raise ValidationError("This file has an invalid size.")
    return upload


class VideoAdminForm(forms.ModelForm):
    """Validate video and thumbnail files submitted through Django Admin."""

    class Meta:
        """Bind the admin form to the video catalog model."""

        model = Video
        fields = "__all__"

    def clean_video_file(self):
        """Allow supported video extensions within the configured size cap."""
        return validate_upload(
            self.cleaned_data["video_file"], VIDEO_EXTENSIONS
        )

    def clean_thumbnail(self):
        """Allow common web image extensions for optional thumbnails."""
        return validate_upload(
            self.cleaned_data.get("thumbnail"), THUMBNAIL_EXTENSIONS
        )


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    """Provide searchable, sortable video management for staff users."""

    form = VideoAdminForm
    list_display = ("title", "category", "status", "created_at")
    list_filter = ("category", "status")
    search_fields = ("title", "description", "category")
    readonly_fields = ("created_at",)
    ordering = ("-created_at", "id")
