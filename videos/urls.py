"""URL patterns for video catalog and protected thumbnail access."""

from django.urls import path

from videos.views import VideoListAPIView, VideoThumbnailAPIView

urlpatterns = [
    path("video/", VideoListAPIView.as_view(), name="video_list"),
    path(
        "video/<int:video_id>/thumbnail/",
        VideoThumbnailAPIView.as_view(),
        name="video_thumbnail",
    ),
]
