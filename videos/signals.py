"""Invalidate video catalog cache after committed model changes."""

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from videos.cache import invalidate_video_catalog_cache
from videos.models import Video


@receiver(post_save, sender=Video)
def invalidate_catalog_after_save(sender, **kwargs):
    """Delete stale catalog data after a video save commits."""
    transaction.on_commit(invalidate_video_catalog_cache)


@receiver(post_delete, sender=Video)
def invalidate_catalog_after_delete(sender, **kwargs):
    """Delete stale catalog data after a video deletion commits."""
    transaction.on_commit(invalidate_video_catalog_cache)
