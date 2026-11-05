"""Serializers for public video catalog representations."""

from django.urls import reverse
from rest_framework import serializers

from videos.models import Video


class VideoSerializer(serializers.ModelSerializer):
    """Expose the metadata required by the official video dashboard."""

    thumbnail_url = serializers.SerializerMethodField()

    class Meta:
        """Select the stable public catalog fields."""

        model = Video
        fields = (
            "id",
            "created_at",
            "title",
            "description",
            "thumbnail_url",
            "category",
        )

    def get_thumbnail_url(self, video):
        """Return a protected thumbnail URL or null before one exists."""
        if not video.thumbnail:
            return None
        url = reverse("video_thumbnail", kwargs={"video_id": video.pk})
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url
