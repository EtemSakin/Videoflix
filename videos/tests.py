"""Tests for the video catalog model."""

from django.test import TestCase

from videos.models import Video


class VideoModelTests(TestCase):
    """Verify catalog metadata and lifecycle model defaults."""

    def create_video(self, **overrides):
        """Create a catalog video with valid representative metadata."""
        data = {
            "title": "A sample film",
            "description": "A short description.",
            "category": "Drama",
            "video_file": "videos/source/sample.mp4",
        }
        data.update(overrides)
        return Video.objects.create(**data)

    def test_video_stores_frontend_catalog_fields(self):
        """Keep every required dashboard value on the video model."""
        video = self.create_video()
        self.assertEqual(video.title, "A sample film")
        self.assertEqual(video.description, "A short description.")
        self.assertEqual(video.category, "Drama")
        self.assertIsNotNone(video.created_at)

    def test_video_defaults_to_pending_processing_state(self):
        """Mark new uploads pending until background conversion completes."""
        video = self.create_video()
        self.assertEqual(video.status, Video.STATUS_PENDING)

    def test_video_statuses_cover_processing_lifecycle(self):
        """Offer pending, processing, ready, and failed states."""
        statuses = {value for value, _label in Video.STATUS_CHOICES}
        self.assertEqual(
            statuses,
            {"pending", "processing", "ready", "failed"},
        )

    def test_catalog_orders_newest_first_deterministically(self):
        """Sort catalog results newest-first with a stable ID tie-breaker."""
        self.assertEqual(Video._meta.ordering, ("-created_at", "id"))

    def test_thumbnail_is_optional_and_has_a_dedicated_media_path(self):
        """Allow thumbnail generation to follow the initial upload."""
        thumbnail = Video._meta.get_field("thumbnail")
        self.assertTrue(thumbnail.blank)
        self.assertEqual(thumbnail.upload_to, "videos/thumbnails/")
