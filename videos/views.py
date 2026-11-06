"""Authenticated views for video catalog and thumbnail access."""

from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from videos.cache import (
    absolute_thumbnail_urls,
    get_video_catalog_data,
)
from videos.models import Video
from videos.transcoding import get_hls_profile
from videos.utils import (
    HLS_SEGMENT_PATTERN,
    manifest_response,
    segment_response,
    thumbnail_response,
)


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
