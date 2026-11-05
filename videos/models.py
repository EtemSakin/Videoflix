"""Database models for the Videoflix video catalog."""

from django.db import models


class Video(models.Model):
    """Store a video's catalog metadata and processing file references."""

    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_READY = "ready"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_READY, "Ready"),
        (STATUS_FAILED, "Failed"),
    )

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=100)
    video_file = models.FileField(upload_to="videos/source/")
    thumbnail = models.FileField(
        blank=True, upload_to="videos/thumbnails/"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    status = models.CharField(
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        max_length=16,
        db_index=True,
    )

    class Meta:
        """Keep catalog queries newest-first with deterministic tie order."""

        ordering = ("-created_at", "id")

    def __str__(self):
        """Return the human-readable video title."""
        return self.title
