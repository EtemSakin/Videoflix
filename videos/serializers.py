"""Serializers for public video catalog representations."""

from django.urls import reverse
from django.utils.html import escape
from rest_framework import serializers

from videos.models import Video


class VideoSerializer(serializers.ModelSerializer):
    """Expose the metadata required by the official video dashboard."""

    title = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    category = serializers.SerializerMethodField()
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
        return reverse("video_thumbnail", kwargs={"video_id": video.pk})

    def get_title(self, video):
        """Escape title text before the official frontend inserts HTML."""
        return escape(video.title)

    def get_description(self, video):
        """Escape description text before the frontend renders its markup."""
        return escape(video.description)

    def get_category(self, video):
        """Escape category text before it reaches frontend HTML."""
        return escape(video.category)
