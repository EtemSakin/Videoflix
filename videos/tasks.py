"""Background jobs and queue submission for video processing."""

import logging
import shutil
from pathlib import Path

import django_rq
from django.conf import settings
from django.db import transaction
from redis.exceptions import RedisError

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
    outputs = []
    try:
        outputs = convert_video(source, output_root)
        attach_video_thumbnail(video_id, source)
    except Exception:
        cleanup_hls_outputs(outputs, output_root)
        mark_processing_failed(video_id)
        raise
    if not mark_processing_ready(video_id):
        cleanup_hls_outputs(outputs, output_root)
        raise RuntimeError("Video processing state changed unexpectedly.")
    return outputs


def claim_video_processing(video_id):
    """Atomically claim pending work so duplicate RQ jobs cannot transcode."""
    with transaction.atomic():
        video = Video.objects.select_for_update().get(pk=video_id)
        if video.status != Video.STATUS_PENDING:
            return None
        video.status = Video.STATUS_PROCESSING
        video.save(update_fields=("status",))
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
    video = Video.objects.get(pk=video_id)
    video.thumbnail.name = generate_video_thumbnail(source, video_id)
    video.save(update_fields=("thumbnail",))


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


def mark_processing_ready(video_id):
    """Mark active work ready only if its processing claim is still current."""
    return update_video_status(
        video_id, Video.STATUS_PROCESSING, Video.STATUS_READY
    )
