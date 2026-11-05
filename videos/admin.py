"""Admin configuration for secure video catalog management."""

from django import forms
from django.contrib import admin
from django.db import transaction

from videos.models import Video
from videos.tasks import enqueue_video_processing
from videos.validators import (
    validate_plain_text,
    validate_thumbnail_upload,
    validate_video_upload,
)


class VideoAdminForm(forms.ModelForm):
    """Validate video and thumbnail files submitted through Django Admin."""

    class Meta:
        """Bind the admin form to the video catalog model."""

        model = Video
        fields = "__all__"

    def clean_video_file(self):
        """Allow supported video extensions within the configured size cap."""
        return validate_video_upload(self.cleaned_data["video_file"])

    def clean_thumbnail(self):
        """Allow common web image extensions for optional thumbnails."""
        return validate_thumbnail_upload(self.cleaned_data.get("thumbnail"))

    def clean_title(self):
        """Reject HTML markup from video titles rendered by the frontend."""
        return validate_plain_text(self.cleaned_data["title"])

    def clean_description(self):
        """Reject HTML markup from descriptions rendered by the frontend."""
        return validate_plain_text(self.cleaned_data["description"])

    def clean_category(self):
        """Reject HTML markup from category labels."""
        return validate_plain_text(self.cleaned_data["category"])


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    """Provide searchable, sortable video management for staff users."""

    form = VideoAdminForm
    list_display = ("title", "category", "status", "created_at")
    list_filter = ("category", "status")
    search_fields = ("title", "description", "category")
    readonly_fields = ("created_at",)
    ordering = ("-created_at", "id")

    def save_model(self, request, obj, form, change):
        """Queue pending video processing after the database commit."""
        should_queue = obj._state.adding or obj.status == Video.STATUS_PENDING
        super().save_model(request, obj, form, change)
        if should_queue:
            transaction.on_commit(
                lambda: enqueue_video_processing(obj.pk)
            )
