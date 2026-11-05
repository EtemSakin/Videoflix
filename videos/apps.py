"""Application configuration for the video catalog."""

from django.apps import AppConfig


class VideosConfig(AppConfig):
    """Configure the videos application."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "videos"
