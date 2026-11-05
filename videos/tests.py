"""Tests for the video catalog model."""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings

from videos.admin import VideoAdmin, VideoAdminForm
from videos.models import Video

User = get_user_model()


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


class VideoAdminTests(TestCase):
    """Verify video admin integration and upload validation."""

    def admin_form(self, filename, thumbnail=None, content=b"sample"):
        """Build a valid admin form with supplied media uploads."""
        files = {"video_file": SimpleUploadedFile(filename, content)}
        if thumbnail:
            files["thumbnail"] = SimpleUploadedFile(thumbnail, b"image")
        return VideoAdminForm(
            data={
                "title": "Admin film",
                "description": "Description",
                "category": "Drama",
                "status": Video.STATUS_PENDING,
            },
            files=files,
        )

    def test_video_admin_is_registered_with_practical_catalog_columns(self):
        """Expose useful catalog fields and filters in Django Admin."""
        model_admin = admin.site._registry[Video]
        self.assertIsInstance(model_admin, VideoAdmin)
        self.assertEqual(
            model_admin.list_display[0:3],
            ("title", "category", "status"),
        )

    def test_admin_site_requires_active_staff_access(self):
        """Deny regular or inactive users access to Django Admin."""
        request = RequestFactory().get("/admin/")
        request.user = User(email="staff@example.com", is_active=True)
        self.assertFalse(admin.site.has_permission(request))
        request.user.is_staff = True
        self.assertTrue(admin.site.has_permission(request))
        request.user.is_active = False
        self.assertFalse(admin.site.has_permission(request))

    def test_admin_form_accepts_supported_media_extensions(self):
        """Accept a supported video and optional image thumbnail."""
        form = self.admin_form("sample.MP4", "preview.webp")
        self.assertTrue(form.is_valid(), form.errors)

    def test_admin_form_rejects_unsupported_video_extension(self):
        """Reject an executable upload renamed as an unsupported format."""
        form = self.admin_form("payload.exe")
        self.assertFalse(form.is_valid())
        self.assertIn("video_file", form.errors)

    def test_admin_form_rejects_unsupported_thumbnail_extension(self):
        """Reject non-image filename extensions for thumbnails."""
        form = self.admin_form("sample.mp4", "preview.svg")
        self.assertFalse(form.is_valid())
        self.assertIn("thumbnail", form.errors)

    @override_settings(VIDEO_MAX_UPLOAD_BYTES=3)
    def test_admin_form_rejects_oversized_uploads(self):
        """Enforce the configurable upload size limit."""
        form = self.admin_form("sample.mp4", content=b"larger")
        self.assertFalse(form.is_valid())
        self.assertIn("video_file", form.errors)
