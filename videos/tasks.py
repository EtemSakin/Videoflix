"""Background jobs and queue submission for video processing."""

import logging
import shutil
from pathlib import Path

import django_rq
from django.conf import settings
from django.db import transaction
from redis.exceptions import RedisError

from videos.cache import invalidate_video_catalog_cache
from videos.media_cleanup import cleanup_video_artifacts
from videos.models import Video
from videos.thumbnails import generate_video_thumbnail
from videos.transcoding import HLS_PROFILES, transcode_hls_rendition

logger = logging.getLogger(__name__)


def enqueue_video_processing(video_id):
    """Submit a video ID to RQ without exposing media paths in logs."""
    try:
        job = django_rq.get_queue("default").enqueue(
            "videos.tasks.process_video", video_id, job_timeout=3600
        )
        log_queued_job(video_id, job.id)
        return job
    except RedisError:
        logger.exception(
            "Failed to queue video processing for id=%s", video_id
        )
        mark_pending_failed(video_id)
        return None


def log_queued_job(video_id, job_id):
    """Log identifiers that help staff trace queued processing work."""
    logger.info(
        "Queued video processing for id=%s as job=%s", video_id, job_id
    )


def process_video(video_id):
    """Claim a pending video, transcode it, and persist its final state."""
    claimed = claim_video_processing(video_id)
    if claimed is None:
        return []
    source, output_root = claimed
    return complete_video_processing(video_id, source, output_root)


def complete_video_processing(video_id, source, output_root):
    """Convert media and persist thumbnail and processing status."""
    outputs = process_video_outputs(video_id, source, output_root)
    if outputs is None:
        return []
    if not mark_processing_ready(video_id):
        deleted = handle_lost_processing_claim(
            video_id, source, output_root, outputs
        )
        if deleted:
            return []
        raise RuntimeError("Video processing state changed unexpectedly.")
    return outputs


def process_video_outputs(video_id, source, output_root):
    """Convert HLS files and attach the generated thumbnail safely."""
    outputs = []
    try:
        outputs = convert_video(source, output_root)
        attach_video_thumbnail(video_id, source)
    except Video.DoesNotExist:
        cleanup_deleted_processing(video_id, source, output_root, outputs)
        return None
    except Exception:
        handle_processing_failure(video_id, source, output_root, outputs)
        raise
    return outputs


def handle_processing_failure(video_id, source, output_root, outputs):
    """Clean partial outputs and save a failed or deleted-job outcome."""
    cleanup_hls_outputs(outputs, output_root)
    if video_exists(video_id):
        mark_processing_failed(video_id)
    else:
        cleanup_deleted_video(video_id, source)


def handle_lost_processing_claim(video_id, source, output_root, outputs):
    """Remove generated files when a worker loses its database claim."""
    cleanup_hls_outputs(outputs, output_root)
    if video_exists(video_id):
        return False
    cleanup_deleted_video(video_id, source)
    return True


def cleanup_deleted_processing(video_id, source, output_root, outputs):
    """Finish an intentionally deleted video job without an RQ failure."""
    cleanup_hls_outputs(outputs, output_root)
    cleanup_deleted_video(video_id, source)


def claim_video_processing(video_id):
    """Atomically claim pending work so duplicate RQ jobs cannot transcode."""
    try:
        with transaction.atomic():
            video = Video.objects.select_for_update().get(pk=video_id)
            if video.status != Video.STATUS_PENDING:
                return None
            video.status = Video.STATUS_PROCESSING
            video.save(update_fields=("status",))
    except Video.DoesNotExist:
        return None
    return video.video_file.path, settings.VIDEO_HLS_ROOT / str(video.pk)


def convert_video(source, output_root):
    """Generate all configured HLS profiles for a claimed video."""
    outputs = []
    try:
        for resolution in HLS_PROFILES:
            output = transcode_hls_rendition(source, output_root, resolution)
            outputs.append(str(output))
    except Exception:
        cleanup_hls_outputs(outputs, output_root)
        raise
    return outputs


def cleanup_hls_outputs(outputs, output_root):
    """Remove only completed rendition directories created by this job."""
    root = Path(output_root).resolve()
    for output in outputs:
        path = Path(output)
        if not is_owned_rendition(path, root):
            continue
        try:
            shutil.rmtree(path)
        except OSError:
            logger.exception("Failed to remove partial HLS rendition.")


def is_owned_rendition(path, root):
    """Allow cleanup only for regular profile directories under this video."""
    return (
        path.parent.resolve() == root
        and path.name in HLS_PROFILES
        and not path.is_symlink()
        and path.is_dir()
    )


def attach_video_thumbnail(video_id, source):
    """Store the generated private thumbnail on its catalog row."""
    Video.objects.get(pk=video_id)
    name = generate_video_thumbnail(source, video_id)
    updated = Video.objects.filter(
        pk=video_id, status=Video.STATUS_PROCESSING
    ).update(thumbnail=name)
    if not updated:
        raise Video.DoesNotExist
    transaction.on_commit(invalidate_video_catalog_cache)


def video_exists(video_id):
    """Return whether a worker's catalog row still exists."""
    return Video.objects.filter(pk=video_id).exists()


def cleanup_deleted_video(video_id, source):
    """Finish cleanup after a running worker releases deleted media."""
    try:
        source_name = Path(source).resolve().relative_to(
            Path(settings.MEDIA_ROOT).resolve()
        ).as_posix()
    except (OSError, ValueError):
        source_name = None
    thumbnail_name = f"videos/thumbnails/{video_id}/thumbnail.jpg"
    cleanup_video_artifacts(video_id, source_name, thumbnail_name)


def update_video_status(video_id, expected_status, new_status):
    """Apply a lifecycle transition only while the expected state remains."""
    with transaction.atomic():
        video = Video.objects.select_for_update().filter(
            pk=video_id, status=expected_status
        ).first()
        if video is None:
            return False
        video.status = new_status
        video.save(update_fields=("status",))
    return True


def mark_processing_failed(video_id):
    """Mark active work as failed after conversion raises an error."""
    update_video_status(video_id, Video.STATUS_PROCESSING, Video.STATUS_FAILED)


def mark_pending_failed(video_id):
    """Mark queue submission failures so they cannot look pending forever."""
    update_video_status(video_id, Video.STATUS_PENDING, Video.STATUS_FAILED)


def mark_processing_ready(video_id):
    """Mark active work ready only if its processing claim is still current."""
    return update_video_status(
        video_id, Video.STATUS_PROCESSING, Video.STATUS_READY
    )
