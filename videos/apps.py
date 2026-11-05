"""Application configuration for the video catalog."""

from importlib import import_module

from django.apps import AppConfig


class VideosConfig(AppConfig):
    """Configure the videos application."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "videos"

    def ready(self):
        """Connect model signals when Django loads the app."""
        import_module("videos.signals")
