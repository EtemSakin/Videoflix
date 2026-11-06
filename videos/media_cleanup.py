"""Safely remove private media files owned by deleted or replaced videos."""

import logging
import shutil
from pathlib import Path

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)


def cleanup_video_artifacts(video_id, source_name=None, thumbnail_name=None):
    """Remove managed files and the video HLS directory within media roots."""
    remove_stored_file(source_name, settings.VIDEO_SOURCE_ROOT)
    remove_stored_file(thumbnail_name, settings.VIDEO_THUMBNAIL_ROOT)
    remove_hls_directory(video_id)


def remove_stored_file(name, allowed_root):
    """Delete a stored file only when its resolved path is under its root."""
    if not name:
        return
    paths = managed_file_paths(name, allowed_root)
    if paths is not None:
        unlink_managed_file(paths[1])


def managed_file_paths(name, allowed_root):
    """Resolve a stored file and reject paths outside private media roots."""
    try:
        return resolve_managed_file(name, allowed_root)
    except (
        OSError,
        ValueError,
        NotImplementedError,
        SuspiciousFileOperation,
    ):
        return None


def resolve_managed_file(name, allowed_root):
    """Return a verified media path and its managed root."""
    media_root = Path(settings.MEDIA_ROOT).resolve(strict=True)
    root = Path(allowed_root).resolve(strict=True)
    path = Path(default_storage.path(name))
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_file():
        return None
    if not root.is_relative_to(media_root):
        return None
    if not resolved.is_relative_to(root):
        return None
    return root, resolved


def remove_hls_directory(video_id):
    """Remove only the selected video's real HLS directory under media root."""
    try:
        media_root = Path(settings.MEDIA_ROOT).resolve(strict=True)
        hls_root = Path(settings.VIDEO_HLS_ROOT).resolve(strict=True)
        directory = hls_root / str(video_id)
        resolved = directory.resolve(strict=True)
    except (OSError, RuntimeError):
        return
    if resolved != directory or not resolved.is_dir():
        return
    if hls_root.is_relative_to(media_root) and resolved.parent == hls_root:
        remove_managed_directory(resolved)


def unlink_managed_file(path):
    """Unlink a contained file and log cleanup failures."""
    try:
        path.unlink()
    except OSError:
        logger.exception("Failed to remove managed video media file.")


def remove_managed_directory(path):
    """Remove an owned generated directory and log filesystem failures."""
    try:
        shutil.rmtree(path)
    except OSError:
        logger.exception("Failed to remove managed HLS output directory.")
