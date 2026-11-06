"""Admin configuration for secure video catalog management."""

from django import forms
from django.contrib import admin
from django.db import transaction

from videos.media_cleanup import cleanup_video_artifacts
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
    actions = ("retry_failed_processing",)

    def save_model(self, request, obj, form, change):
        """Queue new or replaced source media after the database commit."""
        old_media = self.previous_media(obj, form, change)
        should_queue = self.should_queue_video(obj, form, change)
        if old_media:
            obj.status = Video.STATUS_PENDING
            obj.thumbnail = ""
        super().save_model(request, obj, form, change)
        if should_queue:
            self.schedule_processing(obj.pk, old_media)

    def previous_media(self, obj, form, change):
        """Return prior file names when an existing source is replaced."""
        if not change or form is None or "video_file" not in form.changed_data:
            return None
        previous = Video.objects.get(pk=obj.pk)
        return previous.video_file.name, previous.thumbnail.name

    def should_queue_video(self, obj, form, change):
        """Queue new uploads, source replacements, and pending rows."""
        if obj._state.adding or obj.status == Video.STATUS_PENDING:
            return True
        return bool(
            change and form and "video_file" in form.changed_data
        )

    def schedule_processing(self, video_id, old_media):
        """Clean obsolete outputs and enqueue the committed source file."""
        transaction.on_commit(
            lambda: process_saved_video(video_id, old_media)
        )

    @admin.action(description="Retry processing for failed videos")
    def retry_failed_processing(self, request, queryset):
        """Reset failed videos and queue one traceable processing attempt."""
        for video in queryset.filter(status=Video.STATUS_FAILED):
            self.prepare_retry(video)

    def prepare_retry(self, video):
        """Clear stale preview state and schedule a fresh queue attempt."""
        thumbnail_name = video.thumbnail.name
        video.status = Video.STATUS_PENDING
        video.thumbnail = ""
        video.save(update_fields=("status", "thumbnail"))
        transaction.on_commit(
            lambda: retry_saved_video(video.pk, thumbnail_name)
        )


def process_saved_video(video_id, old_media):
    """Remove replaced media before queueing the new source file."""
    if old_media:
        cleanup_video_artifacts(video_id, *old_media)
    enqueue_video_processing(video_id)


def retry_saved_video(video_id, thumbnail_name):
    """Remove stale HLS and thumbnail files before retrying a failed job."""
    cleanup_video_artifacts(video_id, thumbnail_name=thumbnail_name)
    enqueue_video_processing(video_id)
