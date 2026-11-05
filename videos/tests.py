"""Tests for the video catalog model."""

from datetime import timedelta

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import AccessToken

from videos.admin import VideoAdmin, VideoAdminForm
from videos.models import Video

User = get_user_model()
VIDEO_RESPONSE_FIELDS = {
    "id",
    "created_at",
    "title",
    "description",
    "thumbnail_url",
    "category",
}


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


class PrivateMediaStorageTests(SimpleTestCase):
    """Verify media paths are separated and not publicly routed."""

    def test_source_thumbnail_and_hls_directories_use_media_root(self):
        """Keep generated assets under the configured media volume."""
        root = settings.MEDIA_ROOT
        self.assertEqual(settings.VIDEO_SOURCE_ROOT, root / "videos/source")
        self.assertEqual(
            settings.VIDEO_THUMBNAIL_ROOT, root / "videos/thumbnails"
        )
        self.assertEqual(settings.VIDEO_HLS_ROOT, root / "videos/hls")

    def test_media_url_has_no_public_url_pattern(self):
        """Avoid bypassing authenticated media endpoints with static URLs."""
        with self.assertRaises(Resolver404):
            resolve("/media/videos/hls/1/480p/index.m3u8")

    def test_uploaded_media_receives_restricted_file_permissions(self):
        """Set restrictive file and directory permissions on Unix hosts."""
        self.assertEqual(settings.FILE_UPLOAD_PERMISSIONS, 0o640)
        self.assertEqual(settings.FILE_UPLOAD_DIRECTORY_PERMISSIONS, 0o750)


class VideoListAPITests(APITestCase):
    """Verify the authenticated catalog response contract."""

    def setUp(self):
        """Authenticate a browser-style client with an access cookie."""
        user = User.objects.create_user(
            "catalog@example.com", "StrongPass!73"
        )
        token = str(AccessToken.for_user(user))
        self.client.cookies[settings.JWT_ACCESS_COOKIE_NAME] = token

    def create_video(self, title, status_value, **overrides):
        """Create a video row with a referenced source file."""
        values = {
            "title": title,
            "description": "Catalog description",
            "category": "Drama",
            "video_file": "videos/source/sample.mp4",
            "status": status_value,
        }
        values.update(overrides)
        return Video.objects.create(**values)

    def assert_catalog_response(self, response, expected_ids):
        """Assert the documented JSON array and its exact field names."""
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data, list)
        self.assertEqual([item["id"] for item in response.data], expected_ids)
        self.assertEqual(set(response.data[0]), VIDEO_RESPONSE_FIELDS)

    def assert_thumbnail_url_is_routable(self, video):
        """Return the protected route in the API field for stored images."""
        response = self.client.get(reverse("video_list"))
        expected = f"/api/video/{video.pk}/thumbnail/"
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data[0]["thumbnail_url"].endswith(expected))

    def test_catalog_requires_authentication(self):
        """Require a valid access cookie for the official video endpoint."""
        response = APIClient().get(reverse("video_list"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_catalog_returns_ready_items_newest_first_as_an_array(self):
        """Return the six documented fields and hide unfinished videos."""
        older = self.create_video("Older", Video.STATUS_READY)
        newer = self.create_video("Newer", Video.STATUS_READY)
        self.create_video("Pending", Video.STATUS_PENDING)
        now = timezone.now()
        Video.objects.filter(pk=older.pk).update(
            created_at=now - timedelta(days=1)
        )
        Video.objects.filter(pk=newer.pk).update(created_at=now)
        response = self.client.get(reverse("video_list"))
        self.assert_catalog_response(response, [newer.pk, older.pk])
        self.assertIsNone(response.data[0]["thumbnail_url"])

    def test_missing_thumbnail_file_returns_404_without_breaking_catalog(self):
        """Keep missing disk assets from causing server errors."""
        video = self.create_video(
            "Ready",
            Video.STATUS_READY,
            thumbnail="videos/thumbnails/missing.jpg",
        )
        self.assert_thumbnail_url_is_routable(video)
        response = self.client.get(
            reverse("video_thumbnail", kwargs={"video_id": video.pk})
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_thumbnail_route_requires_authentication(self):
        """Keep thumbnail files behind the same cookie authentication."""
        video = self.create_video("Ready", Video.STATUS_READY)
        path = reverse("video_thumbnail", kwargs={"video_id": video.pk})
        response = APIClient().get(path)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
