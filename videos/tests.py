"""Tests for the video catalog model."""

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.test.utils import CaptureQueriesContext
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone
from redis.exceptions import RedisError
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import AccessToken

from videos.admin import VideoAdmin, VideoAdminForm
from videos.cache import VIDEO_CATALOG_CACHE_KEY
from videos.models import Video
from videos.transcoding import (
    HLS_PROFILES,
    build_hls_command,
    get_hls_profile,
)
from videos.validators import validate_file_name

User = get_user_model()
VIDEO_RESPONSE_FIELDS = {
    "id",
    "created_at",
    "title",
    "description",
    "thumbnail_url",
    "category",
}
ESCAPED_METADATA = {
    "title": "&lt;script&gt;alert(1)&lt;/script&gt;",
    "description": "&lt;img src=x&gt;",
    "category": "&lt;b&gt;Drama&lt;/b&gt;",
}
RESOLUTION_HEIGHTS = (("480p", 480), ("720p", 720), ("1080p", 1080))


def admin_form_data():
    """Return valid metadata for admin form tests."""
    return {
        "title": "Admin film",
        "description": "Description",
        "category": "Drama",
        "status": Video.STATUS_PENDING,
    }


def admin_uploads(filename, thumbnail, content):
    """Build source and optional WebP uploads for admin form tests."""
    files = {"video_file": SimpleUploadedFile(filename, content)}
    if thumbnail:
        files["thumbnail"] = SimpleUploadedFile(
            thumbnail, b"RIFF\x00\x00\x00\x00WEBP"
        )
    return files


def catalog_metadata(item):
    """Select escaped text values from a catalog response item."""
    return {field: item[field] for field in ESCAPED_METADATA}


def hls_output_paths(directory):
    """Return expected segment-template and playlist paths."""
    root = Path(directory).resolve()
    return str(root / "segment_%05d.ts"), str(root / "index.m3u8")


class HLSCommandBuilderTests(SimpleTestCase):
    """Verify rendition profiles and shell-safe FFmpeg arguments."""

    def test_profiles_cover_official_hls_resolutions(self):
        """Expose the three resolutions required by the assignment."""
        self.assertEqual(set(HLS_PROFILES), {"480p", "720p", "1080p"})
        for resolution, height in RESOLUTION_HEIGHTS:
            with self.subTest(resolution=resolution):
                self.assertEqual(get_hls_profile(resolution).height, height)

    def test_builder_returns_argument_list_and_absolute_paths(self):
        """Keep media paths as individual arguments with no shell string."""
        command = build_hls_command("source;touch", "output folder", "720p")
        source_index = command.index("-i") + 1
        segment_index = command.index("-hls_segment_filename") + 1
        expected_source = str(Path("source;touch").resolve())
        self.assertEqual(command[source_index], expected_source)
        expected_paths = hls_output_paths("output folder")
        for actual, expected in zip(
            (command[segment_index], command[-1]), expected_paths
        ):
            self.assertEqual(actual, expected)
        self.assertIn("scale=-2:min(ih\\,720)", command)

    def test_builder_rejects_unsupported_resolution(self):
        """Reject arbitrary resolution text before forming FFmpeg options."""
        with self.assertRaises(ValueError):
            build_hls_command("source.mp4", "output", "720p;touch")


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

    def admin_form(
        self,
        filename,
        thumbnail=None,
        content=b"\x00\x00\x00\x18ftypisom",
    ):
        """Build a valid admin form with supplied media uploads."""
        return VideoAdminForm(
            data=admin_form_data(),
            files=admin_uploads(filename, thumbnail, content),
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

    def test_admin_form_rejects_content_with_a_mismatched_signature(self):
        """Reject renamed files whose content does not match the extension."""
        form = self.admin_form("payload.mp4", content=b"not a video")
        self.assertFalse(form.is_valid())
        self.assertIn("video_file", form.errors)

    def test_admin_form_rejects_empty_uploads(self):
        """Reject zero-byte source uploads."""
        form = self.admin_form("empty.mp4", content=b"")
        self.assertFalse(form.is_valid())
        self.assertIn("video_file", form.errors)

    def test_admin_form_rejects_markup_in_video_metadata(self):
        """Reject markup in fields shown as HTML by the frontend."""
        form = self.admin_form("sample.mp4")
        form.data = {**form.data, "title": "<script>alert(1)</script>"}
        self.assertFalse(form.is_valid())
        self.assertIn("title", form.errors)

    def test_filename_validator_rejects_path_traversal(self):
        """Reject path separators and null bytes in upload names."""
        for filename in ("../film.mp4", "folder/film.mp4", "bad\x00.mp4"):
            with self.subTest(filename=filename):
                with self.assertRaises(ValidationError):
                    validate_file_name(filename)


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
        cache.delete(VIDEO_CATALOG_CACHE_KEY)
        self.addCleanup(cache.delete, VIDEO_CATALOG_CACHE_KEY)
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

    def test_catalog_escapes_metadata_before_frontend_html_insertion(self):
        """Escape stored markup even when data bypassed the admin form."""
        video = self.create_video(
            "<script>alert(1)</script>",
            Video.STATUS_READY,
            description="<img src=x>",
            category="<b>Drama</b>",
        )
        response = self.client.get(reverse("video_list"))
        self.assertEqual(response.data[0]["id"], video.pk)
        self.assertEqual(catalog_metadata(response.data[0]), ESCAPED_METADATA)

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

    def test_redis_cache_avoids_repeating_catalog_database_query(self):
        """Use Redis after the first catalog query."""
        self.create_video("Cached", Video.STATUS_READY)
        with CaptureQueriesContext(connection) as first_queries:
            self.client.get(reverse("video_list"))
        with CaptureQueriesContext(connection) as cached_queries:
            response = self.client.get(reverse("video_list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(count_catalog_queries(first_queries), 1)
        self.assertEqual(count_catalog_queries(cached_queries), 0)

    def test_committed_video_changes_invalidate_redis_catalog(self):
        """Refresh cached fields after save and remove data after deletion."""
        video = self.create_video("Before", Video.STATUS_READY)
        self.client.get(reverse("video_list"))
        with self.captureOnCommitCallbacks(execute=True):
            video.title = "After"
            video.save(update_fields=("title",))
        updated = self.client.get(reverse("video_list"))
        self.assertEqual(updated.data[0]["title"], "After")
        with self.captureOnCommitCallbacks(execute=True):
            video.delete()
        self.assertEqual(self.client.get(reverse("video_list")).data, [])

    def test_catalog_falls_back_to_database_when_redis_is_unavailable(self):
        """Keep the catalog available through temporary Redis failures."""
        self.create_video("Fallback", Video.STATUS_READY)
        with patch("videos.cache.cache.get", side_effect=RedisError):
            with patch("videos.cache.cache.set", side_effect=RedisError):
                response = self.client.get(reverse("video_list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["title"], "Fallback")


def count_catalog_queries(captured_queries):
    """Count only database statements that read the video table."""
    return sum(
        "videos_video" in query["sql"] for query in captured_queries
    )
