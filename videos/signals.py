"""Invalidate video catalog cache after committed model changes."""

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_delete
from django.dispatch import receiver

from videos.cache import invalidate_video_catalog_cache
from videos.media_cleanup import cleanup_video_artifacts
from videos.models import Video


@receiver(pre_delete, sender=Video)
def lock_video_state_before_delete(sender, instance, **kwargs):
    """Serialize deletion with worker claims and capture the current status."""
    row = Video.objects.select_for_update().filter(pk=instance.pk).first()
    if row is not None:
        instance.status = row.status


@receiver(post_save, sender=Video)
def invalidate_catalog_after_save(sender, **kwargs):
    """Delete stale catalog data after a video save commits."""
    transaction.on_commit(invalidate_video_catalog_cache)


@receiver(post_delete, sender=Video)
def invalidate_catalog_after_delete(sender, **kwargs):
    """Delete stale catalog data after a video deletion commits."""
    transaction.on_commit(invalidate_video_catalog_cache)


@receiver(post_delete, sender=Video)
def cleanup_media_after_delete(sender, instance, **kwargs):
    """Clean deleted media after commit unless an active worker owns it."""
    if instance.status == Video.STATUS_PROCESSING:
        return
    video_id = instance.pk
    source_name = instance.video_file.name
    thumbnail_name = instance.thumbnail.name
    transaction.on_commit(
        lambda: cleanup_video_artifacts(
            video_id,
            source_name,
            thumbnail_name,
        )
    )
