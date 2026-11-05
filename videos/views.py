"""Authenticated views for video catalog and thumbnail access."""

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

THUMBNAIL_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


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
    response.headers.update(
        {
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        }
    )
    return response
