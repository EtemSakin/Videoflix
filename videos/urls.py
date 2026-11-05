"""URL patterns for video catalog and protected thumbnail access."""

from django.urls import path

from videos.views import (
    VideoListAPIView,
    VideoManifestAPIView,
    VideoThumbnailAPIView,
)

urlpatterns = [
    path("video/", VideoListAPIView.as_view(), name="video_list"),
    path(
        "video/<int:video_id>/thumbnail/",
        VideoThumbnailAPIView.as_view(),
        name="video_thumbnail",
    ),
    path(
        "video/<int:movie_id>/<str:resolution>/index.m3u8",
        VideoManifestAPIView.as_view(),
        name="video_manifest",
    ),
]
