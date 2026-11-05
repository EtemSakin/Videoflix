"""Tests for the video catalog model."""

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import partial
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import django_rq
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.db.models.fields.files import FieldFile
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
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
from videos.tasks import enqueue_video_processing, process_video
from videos.thumbnails import (
    ThumbnailGenerationError,
    build_thumbnail_command,
    generate_video_thumbnail,
)
from videos.transcoding import (
    HLS_PROFILES,
    FFmpegUnavailableError,
    HLSConversionError,
    build_hls_command,
    get_hls_profile,
    transcode_hls_rendition,
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


def new_admin_video():
    """Build a saved admin video fixture with a small source upload."""
    return Video(
        title="Queued film",
        description="Description",
        category="Drama",
        video_file=SimpleUploadedFile(
            "source.mp4", b"\x00\x00\x00\x18ftypisom"
        ),
    )


def create_concurrent_video():
    """Create the database row shared by duplicate worker connections."""
    return Video.objects.create(
        title="Concurrent film",
        category="Drama",
        video_file="videos/source/concurrent.mp4",
    )


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


def fake_ffmpeg_success(command, **options):
    """Write representative HLS artifacts for process-wrapper tests."""
    segment_pattern = Path(command[command.index("-hls_segment_filename") + 1])
    segment_pattern.parent.mkdir(parents=True, exist_ok=True)
    (segment_pattern.parent / "segment_00000.ts").write_bytes(b"segment")
    Path(command[-1]).write_text("#EXTM3U\nsegment_00000.ts\n")
    return subprocess.CompletedProcess(command, 0)


def mock_hls_output(_source, root, resolution):
    """Return the expected profile path in mocked worker tests."""
    return root / resolution


def fake_thumbnail_success(command, **options):
    """Write a representative JPEG for thumbnail process tests."""
    Path(command[-1]).write_bytes(b"\xff\xd8\xffpreview")
    return subprocess.CompletedProcess(command, 0)


def process_video_in_thread(video_id):
    """Use an independent database connection for each worker thread."""
    close_old_connections()
    try:
        return process_video(video_id)
    finally:
        close_old_connections()


def block_first_conversion(
    started, release, calls, lock, _source, root, resolution
):
    """Hold the first worker inside conversion while a duplicate starts."""
    with lock:
        is_first = not calls
        calls.append(resolution)
    if is_first:
        started.set()
        release.wait(timeout=10)
    return root / resolution


def wait_for_first_worker(started, release):
    """Require the initial worker to enter conversion before the duplicate."""
    if not started.wait(timeout=10):
        release.set()
        raise TimeoutError("The first worker did not start conversion.")


def run_duplicate_video_jobs(
    video_id, converter, started, release
):
    """Run two independent jobs while the first one owns the claim."""
    with patch("videos.tasks.transcode_hls_rendition", side_effect=converter):
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(process_video_in_thread, video_id)
            wait_for_first_worker(started, release)
            second = executor.submit(process_video_in_thread, video_id)
            duplicate = second.result(timeout=10)
            release.set()
            completed = first.result(timeout=10)
    return completed, duplicate


def run_concurrent_conversion(video_id):
    """Run two duplicate workers with the first held during conversion."""
    started = threading.Event()
    release = threading.Event()
    calls = []
    converter = partial(
        block_first_conversion, started, release, calls, threading.Lock()
    )
    completed, duplicate = run_duplicate_video_jobs(
        video_id, converter, started, release
    )
    return completed, duplicate, calls


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
        self.assertIn("scale=-2:720", command)

    def test_builder_rejects_unsupported_resolution(self):
        """Reject arbitrary resolution text before forming FFmpeg options."""
        with self.assertRaises(ValueError):
            build_hls_command("source.mp4", "output", "720p;touch")

    def test_successful_conversion_publishes_manifest_and_segments(self):
        """Publish a rendition only after FFmpeg produced its artifacts."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            with patch(
                "videos.transcoding.subprocess.run",
                side_effect=fake_ffmpeg_success,
            ) as run:
                output = transcode_hls_rendition(source, root / "hls", "480p")
                self.assertTrue((output / "index.m3u8").is_file())
                self.assertTrue((output / "segment_00000.ts").is_file())
                self.assertFalse(run.call_args.kwargs["shell"])

    def test_missing_ffmpeg_is_reported_and_staging_is_removed(self):
        """Translate a missing executable without leaving partial output."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            with patch(
                "videos.transcoding.subprocess.run",
                side_effect=FileNotFoundError,
            ):
                with self.assertRaises(FFmpegUnavailableError):
                    transcode_hls_rendition(source, root / "hls", "480p")
            self.assertEqual(list((root / "hls").iterdir()), [])

    def test_failed_ffmpeg_process_does_not_publish_partial_output(self):
        """Discard staged files when FFmpeg exits unsuccessfully."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            error = subprocess.CalledProcessError(1, "ffmpeg")
            with patch("videos.transcoding.subprocess.run", side_effect=error):
                with self.assertRaises(HLSConversionError):
                    transcode_hls_rendition(source, root / "hls", "480p")
            self.assertFalse((root / "hls" / "480p").exists())


class ThumbnailGenerationTests(SimpleTestCase):
    """Verify thumbnail paths, safe arguments, and temporary output cleanup."""

    def thumbnail_source(self):
        """Create temporary source and media paths for thumbnail tests."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "source.mp4"
        source.write_bytes(b"source")
        media_root = root / "media"
        self.enterContext(override_settings(
            MEDIA_ROOT=media_root,
            VIDEO_THUMBNAIL_ROOT=media_root / "videos" / "thumbnails",
        ))
        return source, media_root

    def test_builder_uses_absolute_paths_and_a_single_jpeg_frame(self):
        """Keep user-controlled paths in argv and select a preview frame."""
        command = build_thumbnail_command("source;touch", "output/preview.jpg")
        self.assertIn(str(Path("source;touch").resolve()), command)
        output_path = str(Path("output/preview.jpg").resolve())
        self.assertEqual(command[-1], output_path)
        self.assertIn("thumbnail,scale=640:-2", command)
        self.assertEqual(command[command.index("-frames:v") + 1], "1")

    def test_generation_rejects_thumbnail_paths_outside_media_root(self):
        """Prevent thumbnail configuration from escaping private storage."""
        source, media_root = self.thumbnail_source()
        self.enterContext(
            override_settings(
                VIDEO_THUMBNAIL_ROOT=media_root.parent / "outside"
            )
        )
        with self.assertRaises(ThumbnailGenerationError):
            generate_video_thumbnail(source, 22)

    def test_generation_stores_a_media_relative_name_and_reuses_output(self):
        """Publish the JPEG atomically and skip duplicate generation."""
        source, media_root = self.thumbnail_source()
        with patch(
            "videos.transcoding.subprocess.run",
            side_effect=fake_thumbnail_success,
        ) as run:
            name = generate_video_thumbnail(source, 19)
            reused = generate_video_thumbnail(source, 19)
        self.assertEqual(name, "videos/thumbnails/19/thumbnail.jpg")
        self.assertEqual(reused, name)
        self.assertTrue((media_root / name).is_file())
        self.assertEqual(run.call_count, 1)

    def test_missing_ffmpeg_removes_temporary_thumbnail_files(self):
        """Report missing FFmpeg and leave no public-looking partial output."""
        source, media_root = self.thumbnail_source()
        with patch(
            "videos.transcoding.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            with self.assertRaises(FFmpegUnavailableError):
                generate_video_thumbnail(source, 20)
        self.assertEqual(
            list((media_root / "videos" / "thumbnails" / "20").iterdir()),
            [],
        )

    def test_failed_thumbnail_command_is_wrapped_and_cleaned(self):
        """Keep process errors controlled and remove temporary directories."""
        source, media_root = self.thumbnail_source()
        error = subprocess.CalledProcessError(1, "ffmpeg")
        with patch("videos.transcoding.subprocess.run", side_effect=error):
            with self.assertRaises(ThumbnailGenerationError):
                generate_video_thumbnail(source, 21)
        self.assertEqual(
            list((media_root / "videos" / "thumbnails" / "21").iterdir()),
            [],
        )


class VideoQueueTests(TestCase):
    """Verify admin-triggered work is deferred to the RQ worker."""

    def setUp(self):
        """Replace thumbnail FFmpeg with a stable queued-job result."""
        patcher = patch(
            "videos.tasks.generate_video_thumbnail",
            return_value="videos/thumbnails/test/thumbnail.jpg",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_admin_enqueues_new_video_only_after_database_commit(self):
        """Register queue submission as an on-commit callback."""
        with TemporaryDirectory() as temporary:
            with override_settings(MEDIA_ROOT=Path(temporary)):
                self.assert_admin_enqueue_deferred()

    def assert_admin_enqueue_deferred(self):
        """Persist a new upload and verify its deferred worker job."""
        video = new_admin_video()
        callbacks = []
        with patch("videos.admin.transaction.on_commit", callbacks.append):
            with patch("videos.tasks.django_rq.get_queue") as get_queue:
                admin.site._registry[Video].save_model(
                    None, video, None, False
                )
                get_queue.assert_not_called()
                for callback in callbacks:
                    callback()
                self.assert_job_enqueued(get_queue, video.pk)

    def assert_job_enqueued(self, queue_mock, video_id):
        """Check the RQ task uses an ID and the selected long timeout."""
        queue_mock.return_value.enqueue.assert_called_once_with(
            "videos.tasks.process_video", video_id, job_timeout=3600
        )

    def test_queue_unavailability_is_reported_without_raising(self):
        """Handle Redis enqueue errors without breaking admin uploads."""
        with self.assertLogs("videos.tasks", level="ERROR"):
            with patch(
                "videos.tasks.django_rq.get_queue", side_effect=RedisError
            ):
                self.assertIsNone(enqueue_video_processing(27))

    def test_processing_task_enqueues_each_supported_rendition(self):
        """Process one row through every configured HLS profile."""
        video = Video.objects.create(
            title="Worker film",
            category="Drama",
            video_file="videos/source/worker.mp4",
        )
        with patch(
            "videos.tasks.transcode_hls_rendition", side_effect=mock_hls_output
        ) as transcode:
            outputs = process_video(video.pk)
        self.assertEqual(len(outputs), 3)
        self.assertEqual(transcode.call_count, 3)


class VideoProcessingStatusTests(TestCase):
    """Verify worker jobs persist valid video processing transitions."""

    def setUp(self):
        """Keep status tests independent of the FFmpeg thumbnail binary."""
        patcher = patch(
            "videos.tasks.generate_video_thumbnail",
            return_value="videos/thumbnails/test/thumbnail.jpg",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def create_pending_video(self):
        """Create a row that is eligible for one conversion job."""
        return Video.objects.create(
            title="Pending film",
            category="Drama",
            video_file="videos/source/pending.mp4",
        )

    def test_successful_processing_marks_video_ready(self):
        """Mark ready only after all HLS profiles return successfully."""
        video = self.create_pending_video()
        with patch("videos.tasks.transcode_hls_rendition") as transcode:
            transcode.side_effect = lambda _source, root, size: root / size
            outputs = process_video(video.pk)
        video.refresh_from_db()
        self.assertEqual(video.status, Video.STATUS_READY)
        self.assertEqual(len(outputs), 3)
        self.assertEqual(
            video.thumbnail.name,
            "videos/thumbnails/test/thumbnail.jpg",
        )

    def test_thumbnail_failure_marks_video_failed(self):
        """Do not expose completed HLS output without its required preview."""
        video = self.create_pending_video()
        with patch(
            "videos.tasks.transcode_hls_rendition",
            side_effect=mock_hls_output,
        ):
            with patch(
                "videos.tasks.generate_video_thumbnail",
                side_effect=ThumbnailGenerationError,
            ):
                with self.assertRaises(ThumbnailGenerationError):
                    process_video(video.pk)
        self.assert_video_failed(video)

    def assert_video_failed(self, video):
        """Refresh and verify a worker failure was saved to PostgreSQL."""
        video.refresh_from_db()
        self.assertEqual(video.status, Video.STATUS_FAILED)

    def test_status_transitions_schedule_catalog_cache_invalidation(self):
        """Invalidate cached listings when processing status is saved."""
        video = self.create_pending_video()
        with patch("videos.signals.invalidate_video_catalog_cache") as clear:
            with patch(
                "videos.tasks.transaction.on_commit",
                side_effect=lambda callback: callback(),
            ):
                with patch(
                    "videos.tasks.transcode_hls_rendition",
                    side_effect=mock_hls_output,
                ):
                    process_video(video.pk)
        self.assertEqual(clear.call_count, 3)

    def test_conversion_failure_marks_video_failed(self):
        """Persist failure before returning the worker exception to RQ."""
        video = self.create_pending_video()
        with patch(
            "videos.tasks.transcode_hls_rendition",
            side_effect=HLSConversionError,
        ):
            with self.assertRaises(HLSConversionError):
                process_video(video.pk)
        video.refresh_from_db()
        self.assertEqual(video.status, Video.STATUS_FAILED)

    def test_partial_conversion_cleanup_allows_a_retry(self):
        """Remove completed profiles after failure so a retry can succeed."""
        video = self.create_pending_video()
        with TemporaryDirectory() as temporary:
            hls_root = Path(temporary) / "hls"
            with override_settings(VIDEO_HLS_ROOT=hls_root):
                self.assert_partial_failure_is_cleaned(video, hls_root)
                self.retry_failed_video(video, hls_root)

    def assert_partial_failure_is_cleaned(self, video, hls_root):
        """Verify failure status and cleanup of the first profile."""
        with patch(
            "videos.tasks.transcode_hls_rendition",
            side_effect=self.fail_after_first_profile,
        ):
            with self.assertRaises(HLSConversionError):
                process_video(video.pk)
        self.assert_video_failed(video)
        self.assertFalse((hls_root / str(video.pk) / "480p").exists())

    def fail_after_first_profile(self, source, output_root, resolution):
        """Publish one fake profile, then simulate FFmpeg failure."""
        if resolution != "480p":
            raise HLSConversionError("Simulated profile failure.")
        output = Path(output_root) / resolution
        output.mkdir(parents=True)
        (output / "index.m3u8").write_text("#EXTM3U\n")
        return output

    def retry_failed_video(self, video, hls_root):
        """Reset the failed row and verify all profiles can be published."""
        Video.objects.filter(pk=video.pk).update(status=Video.STATUS_PENDING)
        with patch(
            "videos.tasks.transcode_hls_rendition",
            side_effect=self.create_mock_profile,
        ):
            process_video(video.pk)
        video.refresh_from_db()
        self.assertEqual(video.status, Video.STATUS_READY)
        self.assertTrue((hls_root / str(video.pk) / "480p").is_dir())

    def create_mock_profile(self, source, output_root, resolution):
        """Create one empty rendition folder for a controlled retry test."""
        output = Path(output_root) / resolution
        output.mkdir(parents=True)
        return output

    def test_duplicate_processing_job_skips_non_pending_video(self):
        """Avoid rerunning a job already claimed by another worker."""
        video = self.create_pending_video()
        Video.objects.filter(pk=video.pk).update(
            status=Video.STATUS_PROCESSING
        )
        with patch("videos.tasks.transcode_hls_rendition") as transcode:
            self.assertEqual(process_video(video.pk), [])
        transcode.assert_not_called()


class VideoProcessingConcurrencyTests(TransactionTestCase):
    """Verify concurrent PostgreSQL workers cannot double-claim a video."""

    def setUp(self):
        """Replace thumbnail generation in concurrent worker tests."""
        patcher = patch(
            "videos.tasks.generate_video_thumbnail",
            return_value="videos/thumbnails/test/thumbnail.jpg",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_duplicate_jobs_transcode_only_once_across_connections(self):
        """Lock the pending row while a second worker attempts to claim it."""
        video = create_concurrent_video()
        completed, duplicate, calls = run_concurrent_conversion(video.pk)
        video.refresh_from_db()
        self.assertEqual(len(completed), 3)
        self.assertEqual(duplicate, [])
        self.assertEqual(len(calls), 3)
        self.assertEqual(video.status, Video.STATUS_READY)


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
        self.assertEqual(response["Cache-Control"], "private, no-store")
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

    def test_thumbnail_response_sets_private_file_headers(self):
        """Return a protected image with private caching and MIME headers."""
        video = self.create_video("Preview", Video.STATUS_READY)
        video.thumbnail = "videos/thumbnails/preview.jpg"
        video.save(update_fields=("thumbnail",))
        with patch.object(FieldFile, "open", return_value=BytesIO(b"jpeg")):
            response = self.client.get(
                reverse("video_thumbnail", kwargs={"video_id": video.pk})
            )
        assert_private_file_response(
            self, response, "image/jpeg", "private, max-age=300"
        )

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


def assert_private_file_response(test_case, response, content_type, cache):
    """Assert shared cache and MIME headers on protected file responses."""
    test_case.assertEqual(response.status_code, 200)
    test_case.assertEqual(response["Content-Type"], content_type)
    test_case.assertEqual(response["Cache-Control"], cache)
    test_case.assertEqual(response["X-Content-Type-Options"], "nosniff")
    test_case.assertTrue(response.streaming)
    test_case.assertTrue(response.has_header("Content-Length"))


def assert_manifest_response(test_case, response):
    """Assert private response headers and the exact generated playlist."""
    assert_private_file_response(
        test_case,
        response,
        "application/vnd.apple.mpegurl",
        "private, no-store",
    )
    test_case.assertEqual(
        b"".join(response.streaming_content), b"#EXTM3U\n#EXT-X-ENDLIST\n"
    )


class VideoManifestAPITests(APITestCase):
    """Verify authenticated HLS manifest access and private file handling."""

    def setUp(self):
        """Configure isolated media paths and a cookie-authenticated user."""
        self.configure_manifest_storage()
        self.authenticate_manifest_client()

    def configure_manifest_storage(self):
        """Set temporary private paths for generated HLS assets."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.media_root = Path(temporary.name) / "media"
        self.hls_root = self.media_root / "videos" / "hls"
        self.enterContext(override_settings(
            MEDIA_ROOT=self.media_root,
            VIDEO_HLS_ROOT=self.hls_root,
        ))

    def authenticate_manifest_client(self):
        """Set the project's access cookie for private manifest requests."""
        user = User.objects.create_user(
            "manifest@example.com", "StrongPass!73"
        )
        token = AccessToken.for_user(user)
        self.client.cookies[settings.JWT_ACCESS_COOKIE_NAME] = str(token)

    def create_video(self, status_value=Video.STATUS_READY):
        """Create a video row without writing a source media file."""
        return Video.objects.create(
            title="Manifest film",
            category="Drama",
            video_file="videos/source/manifest.mp4",
            status=status_value,
        )

    def manifest_url(self, video, resolution="480p"):
        """Return the official HLS manifest endpoint for a video row."""
        return reverse(
            "video_manifest",
            kwargs={"movie_id": video.pk, "resolution": resolution},
        )

    def write_manifest(self, video, resolution="480p"):
        """Write a small local HLS manifest under the configured media root."""
        path = self.hls_root / str(video.pk) / resolution / "index.m3u8"
        path.parent.mkdir(parents=True)
        path.write_text("#EXTM3U\n#EXT-X-ENDLIST\n")
        return path

    def segment_url(self, video, segment, resolution="480p", trailing=True):
        """Return a segment URL with or without the documented final slash."""
        route = "video_segment" if trailing else "video_segment_no_slash"
        return reverse(route, kwargs={
            "movie_id": video.pk,
            "resolution": resolution,
            "segment": segment,
        })

    def write_segment(self, video, segment="segment_00000.ts"):
        """Write one sample binary transport-stream artifact."""
        path = self.hls_root / str(video.pk) / "480p" / segment
        path.parent.mkdir(parents=True)
        path.write_bytes(b"mpeg-ts-segment")
        return path

    def test_ready_manifest_returns_private_hls_content(self):
        """Return playlist bytes with the documented MIME type and headers."""
        video = self.create_video()
        self.write_manifest(video)
        response = self.client.get(self.manifest_url(video))
        assert_manifest_response(self, response)

    def test_manifest_route_requires_cookie_authentication(self):
        """Reject anonymous requests to private HLS manifests."""
        video = self.create_video()
        response = APIClient().get(self.manifest_url(video))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_missing_not_ready_and_unsupported_manifests_return_404(self):
        """Hide unavailable media consistently without returning disk paths."""
        ready = self.create_video()
        pending = self.create_video(Video.STATUS_PENDING)
        self.assert_manifest_not_found(ready)
        self.write_manifest(ready)
        self.assert_manifest_not_found(pending)
        self.assert_manifest_not_found(ready, "240p")

    def assert_manifest_not_found(self, video, resolution="480p"):
        """Assert an unavailable rendition is hidden."""
        response = self.client.get(self.manifest_url(video, resolution))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_manifest_symlinks_cannot_escape_the_hls_root(self):
        """Reject a manifest path that resolves outside VIDEO_HLS_ROOT."""
        video = self.create_video()
        outside = self.hls_root.parent / "outside"
        outside.mkdir(parents=True)
        (outside / "index.m3u8").write_text("private")
        rendition = self.hls_root / str(video.pk) / "480p"
        rendition.parent.mkdir(parents=True)
        rendition.symlink_to(outside, target_is_directory=True)
        response = self.client.get(self.manifest_url(video))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_manifest_symlinks_cannot_cross_video_boundaries(self):
        """Reject a rendition directory linked to another video's files."""
        video = self.create_video()
        other_video = self.create_video()
        other_rendition = self.hls_root / str(other_video.pk) / "480p"
        other_rendition.mkdir(parents=True)
        (other_rendition / "index.m3u8").write_text("private")
        rendition = self.hls_root / str(video.pk) / "480p"
        rendition.parent.mkdir(parents=True)
        rendition.symlink_to(other_rendition, target_is_directory=True)
        self.assert_manifest_not_found(video)

    def test_manifest_root_must_remain_inside_media_root(self):
        """Reject a configured HLS directory outside private media storage."""
        video = self.create_video()
        outside = self.media_root.parent / "outside"
        path = outside / str(video.pk) / "480p" / "index.m3u8"
        path.parent.mkdir(parents=True)
        path.write_text("#EXTM3U\n")
        self.enterContext(override_settings(VIDEO_HLS_ROOT=outside))
        self.assert_manifest_not_found(video)

    def test_segment_routes_stream_only_supported_binary_files(self):
        """Serve FFmpeg segments with their private binary response headers."""
        video = self.create_video()
        self.write_segment(video)
        for trailing in (True, False):
            response = self.client.get(
                self.segment_url(video, "segment_00000.ts", trailing=trailing)
            )
            assert_private_file_response(
                self, response, "video/MP2T", "private, no-store"
            )
            body = b"".join(response.streaming_content)
            self.assertEqual(body, b"mpeg-ts-segment")

    def test_segment_route_requires_cookie_authentication(self):
        """Reject anonymous requests for protected HLS transport streams."""
        video = self.create_video()
        response = APIClient().get(
            self.segment_url(video, "segment_00000.ts")
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_missing_unready_and_invalid_segments_return_404(self):
        """Hide absent and unsupported HLS artifacts consistently."""
        ready = self.create_video()
        pending = self.create_video(Video.STATUS_PENDING)
        self.write_segment(ready)
        self.assert_segment_not_found(ready, "missing_00000.ts")
        self.assert_segment_not_found(ready, "segment_00000.ts.bak")
        self.assert_segment_not_found(ready, "segment_00000.ts", "240p")
        self.assert_segment_not_found(pending, "segment_00000.ts")

    def assert_segment_not_found(self, video, segment, resolution="480p"):
        """Assert invalid or unavailable segment requests return 404."""
        response = self.client.get(
            self.segment_url(video, segment, resolution=resolution)
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class VideoPipelineIntegrationTests(TransactionTestCase):
    """Exercise upload, RQ processing, FFmpeg, and protected playback."""

    def setUp(self):
        """Use temporary media and a dedicated Redis database for RQ."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.media_root = Path(temporary.name) / "media"
        self.hls_root = self.media_root / "videos" / "hls"
        self.configure_pipeline_settings()
        self.queue = django_rq.get_queue("default")
        self.queue.empty()
        self.authenticate_client()

    def configure_pipeline_settings(self):
        """Isolate media outputs and queue jobs from development services."""
        self.enterContext(override_settings(
            MEDIA_ROOT=self.media_root,
            VIDEO_HLS_ROOT=self.hls_root,
            VIDEO_THUMBNAIL_ROOT=self.media_root / "videos" / "thumbnails",
            RQ_QUEUES={
                "default": {
                    "URL": "redis://redis:6379/9",
                    "DEFAULT_TIMEOUT": 3600,
                }
            },
        ))

    def authenticate_client(self):
        """Authenticate playback requests with the project's JWT cookie."""
        user = User.objects.create_user(
            "pipeline@example.com", "StrongPass!73"
        )
        token = AccessToken.for_user(user)
        self.client = APIClient()
        self.client.cookies[settings.JWT_ACCESS_COOKIE_NAME] = str(token)

    def create_test_source(self):
        """Generate a short H.264 source fixture with the installed FFmpeg."""
        source = self.media_root.parent / "integration-source.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24",
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100",
            "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(source),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return source

    def upload_video(self, source):
        """Submit a generated upload through the existing Django Admin save."""
        with source.open("rb") as media_file:
            upload = SimpleUploadedFile("sample.mp4", media_file.read())
        video = Video(
            title="Pipeline film", category="Drama", video_file=upload
        )
        VideoAdmin(Video, admin.site).save_model(None, video, None, False)
        return video

    def test_upload_runs_through_rq_ffmpeg_and_protected_hls_playback(self):
        """Test upload conversion and private playback for all profiles."""
        video = self.upload_video(self.create_test_source())
        self.assertEqual(self.queue.count, 1)
        worker = django_rq.get_worker("default")
        self.assertTrue(worker.work(burst=True))
        video.refresh_from_db()
        self.assertEqual(video.status, Video.STATUS_READY)
        self.assert_video_playback(video)

    def assert_video_playback(self, video):
        """Fetch the catalog and private thumbnail, playlists, and segments."""
        catalog = self.client.get(reverse("video_list"))
        self.assertEqual(catalog.status_code, status.HTTP_200_OK)
        self.assert_video_thumbnail(video)
        for resolution in HLS_PROFILES:
            self.assert_hls_rendition(video, resolution)

    def assert_video_thumbnail(self, video):
        """Verify FFmpeg's generated JPEG is served through the API."""
        path = reverse("video_thumbnail", kwargs={"video_id": video.pk})
        response = self.client.get(path)
        assert_private_file_response(
            self, response, "image/jpeg", "private, max-age=300"
        )
        thumbnail = b"".join(response.streaming_content)
        self.assertTrue(thumbnail.startswith(b"\xff\xd8"))

    def assert_hls_rendition(self, video, resolution):
        """Fetch one authenticated manifest and its relative TS segment."""
        path = reverse("video_manifest", kwargs={
            "movie_id": video.pk,
            "resolution": resolution,
        })
        manifest = self.client.get(path)
        assert_private_file_response(
            self,
            manifest,
            "application/vnd.apple.mpegurl",
            "private, no-store",
        )
        self.assert_manifest_segment(video, resolution, manifest)

    def assert_manifest_segment(self, video, resolution, manifest):
        """Read the generated playlist and stream its referenced segment."""
        playlist = b"".join(manifest.streaming_content).decode("utf-8")
        segment = next(
            line for line in playlist.splitlines() if line.endswith(".ts")
        )
        self.assert_segment_stream(video, resolution, segment)

    def assert_segment_stream(self, video, resolution, segment):
        """Fetch the referenced binary segment using the playback API."""
        path = reverse("video_segment", kwargs={
            "movie_id": video.pk,
            "resolution": resolution,
            "segment": segment,
        })
        response = self.client.get(path)
        assert_private_file_response(
            self, response, "video/MP2T", "private, no-store"
        )
        payload = b"".join(response.streaming_content)
        self.assertTrue(payload.startswith(b"\x47"))
