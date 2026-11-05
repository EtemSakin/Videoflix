"""Background jobs and queue submission for video processing."""

import logging

import django_rq
from django.conf import settings
from redis.exceptions import RedisError

from videos.models import Video
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
    """Generate every supported HLS rendition for one saved video."""
    video = Video.objects.get(pk=video_id)
    output_root = settings.VIDEO_HLS_ROOT / str(video.pk)
    return [
        str(
            transcode_hls_rendition(
                video.video_file.path, output_root, resolution
            )
        )
        for resolution in HLS_PROFILES
    ]
