"""Authenticated views for video catalog and thumbnail access."""

import re
from pathlib import Path

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from videos.cache import (
    absolute_thumbnail_urls,
    get_video_catalog_data,
)
from videos.models import Video
from videos.transcoding import get_hls_profile

THUMBNAIL_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
HLS_MANIFEST_CONTENT_TYPE = "application/vnd.apple.mpegurl"
HLS_SEGMENT_CONTENT_TYPE = "video/MP2T"
HLS_SEGMENT_PATTERN = re.compile(r"segment_[0-9]{5}\.ts\Z")


class VideoListAPIView(APIView):
    """List ready videos in the order expected by the dashboard."""

    def get(self, request):
        """Return the cached catalog with URLs for the requesting host."""
        data = get_video_catalog_data()
        response = Response(absolute_thumbnail_urls(data, request))
        response["Cache-Control"] = "private, no-store"
        return response


class VideoThumbnailAPIView(APIView):
    """Serve private thumbnails through cookie-authenticated requests."""

    def get(self, request, video_id):
        """Return a video thumbnail without exposing its storage path."""
        video = get_object_or_404(
            Video, pk=video_id, status=Video.STATUS_READY
        )
        return thumbnail_response(video)


class VideoManifestAPIView(APIView):
    """Serve a ready HLS manifest through cookie-authenticated requests."""

    def get(self, request, movie_id, resolution):
        """Return one supported manifest without exposing its disk path."""
        try:
            get_hls_profile(resolution)
        except ValueError:
            return Response(status=404)
        video = get_object_or_404(
            Video, pk=movie_id, status=Video.STATUS_READY
        )
        return manifest_response(video.pk, resolution)


class VideoSegmentAPIView(APIView):
    """Serve one private transport-stream segment for a ready video."""

    def get(self, request, movie_id, resolution, segment):
        """Return a supported segment without exposing its disk path."""
        try:
            get_hls_profile(resolution)
        except ValueError:
            return Response(status=404)
        if not HLS_SEGMENT_PATTERN.fullmatch(segment):
            return Response(status=404)
        video = get_object_or_404(
            Video, pk=movie_id, status=Video.STATUS_READY
        )
        return segment_response(video.pk, resolution, segment)


def thumbnail_response(video):
    """Open an allowed image or return a safe not-found response."""
    if not video.thumbnail:
        return Response(status=404)
    extension = video.thumbnail.name.rsplit(".", 1)[-1].lower()
    content_type = THUMBNAIL_CONTENT_TYPES.get(f".{extension}")
    if not content_type:
        return Response(status=404)
    try:
        media_file = video.thumbnail.open("rb")
    except (OSError, ValueError, SuspiciousFileOperation):
        return Response(status=404)

    return private_thumbnail_response(media_file, content_type)


def private_thumbnail_response(media_file, content_type):
    """Return an image response with private cache and MIME protections."""
    response = FileResponse(media_file, content_type=content_type)
    response["Cache-Control"] = "private, max-age=300"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def manifest_response(movie_id, resolution):
    """Return a private manifest or a safe not-found response."""
    manifest_path = resolve_manifest_path(movie_id, resolution)
    if manifest_path is None:
        return Response(status=404)
    try:
        media_file = manifest_path.open("rb")
    except OSError:
        return Response(status=404)
    return private_manifest_response(media_file)


def resolve_manifest_path(movie_id, resolution):
    """Resolve an existing manifest while keeping it under VIDEO_HLS_ROOT."""
    return resolve_hls_artifact(movie_id, resolution, "index.m3u8")


def resolve_segment_path(movie_id, resolution, segment):
    """Resolve one existing segment while keeping it in its rendition."""
    if not HLS_SEGMENT_PATTERN.fullmatch(segment):
        return None
    return resolve_hls_artifact(movie_id, resolution, segment)


def resolve_hls_artifact(movie_id, resolution, filename):
    """Resolve a regular HLS asset beneath the configured media directory."""
    root = Path(settings.VIDEO_HLS_ROOT).resolve()
    media_root = Path(settings.MEDIA_ROOT).resolve()
    if not root.is_relative_to(media_root):
        return None
    return resolve_rendition_artifact(root, movie_id, resolution, filename)


def resolve_rendition_artifact(root, movie_id, resolution, filename):
    """Reject symlinks and resolve one asset inside its rendition directory."""
    rendition = root / str(movie_id) / resolution
    candidate = rendition / filename
    try:
        resolved_rendition = rendition.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if resolved_rendition != rendition or resolved != candidate:
        return None
    if not resolved.is_file() or not resolved.is_relative_to(rendition):
        return None
    return resolved


def private_manifest_response(media_file):
    """Return an HLS MIME response with private cache protections."""
    return private_hls_response(media_file, HLS_MANIFEST_CONTENT_TYPE)


def segment_response(movie_id, resolution, segment):
    """Return a private segment stream or a safe not-found response."""
    segment_path = resolve_segment_path(movie_id, resolution, segment)
    if segment_path is None:
        return Response(status=404)
    try:
        media_file = segment_path.open("rb")
    except OSError:
        return Response(status=404)
    return private_hls_response(media_file, HLS_SEGMENT_CONTENT_TYPE)


def private_hls_response(media_file, content_type):
    """Stream private HLS bytes without allowing shared cache storage."""
    response = FileResponse(media_file, content_type=content_type)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
