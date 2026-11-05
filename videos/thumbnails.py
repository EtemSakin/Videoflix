"""Generate and validate stored video preview thumbnails with FFmpeg."""

import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from django.conf import settings

from videos.transcoding import (
    FFmpegUnavailableError,
    execute_ffmpeg,
    validate_source_file,
)


class ThumbnailGenerationError(RuntimeError):
    """Report an invalid or failed thumbnail generation attempt."""


def build_thumbnail_command(input_path, output_path):
    """Build shell-safe FFmpeg arguments for one JPEG preview frame."""
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-i", str(Path(input_path).resolve()),
        "-map", "0:v:0", "-frames:v", "1",
        "-vf", "thumbnail,scale=640:-2", "-q:v", "3", "-an",
        "-map_metadata", "-1",
        str(Path(output_path).resolve()),
    ]


def generate_video_thumbnail(input_path, video_id):
    """Generate a JPEG atomically and return its MEDIA_ROOT-relative name."""
    source = validate_source_file(input_path)
    root = thumbnail_directory(video_id)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / "thumbnail.jpg"
    if existing_thumbnail_is_valid(destination):
        set_thumbnail_permissions(destination, root)
        return thumbnail_name(destination)
    publish_thumbnail(source, root, destination)
    set_thumbnail_permissions(destination, root)
    return thumbnail_name(destination)


def publish_thumbnail(source, root, destination):
    """Generate output in a temporary directory before atomic replacement."""
    with TemporaryDirectory(prefix=".thumbnail-", dir=root) as temporary:
        staged = Path(temporary) / destination.name
        run_thumbnail_command(build_thumbnail_command(source, staged))
        validate_thumbnail_file(staged)
        os.replace(staged, destination)


def set_thumbnail_permissions(destination, root):
    """Apply the backend's private media-file permission settings."""
    os.chmod(destination, settings.FILE_UPLOAD_PERMISSIONS)
    media_root = settings.MEDIA_ROOT.resolve()
    directory = root
    while directory != media_root:
        os.chmod(directory, settings.FILE_UPLOAD_DIRECTORY_PERMISSIONS)
        directory = directory.parent


def thumbnail_directory(video_id):
    """Return the configured private thumbnail folder for one video."""
    directory = settings.VIDEO_THUMBNAIL_ROOT / str(video_id)
    try:
        directory.resolve().relative_to(settings.MEDIA_ROOT.resolve())
    except ValueError as error:
        raise ThumbnailGenerationError(
            "Thumbnail output must remain under MEDIA_ROOT."
        ) from error
    return directory


def existing_thumbnail_is_valid(destination):
    """Reuse complete output and reject unexpected existing filesystem data."""
    if destination.is_symlink():
        raise ThumbnailGenerationError("Thumbnail output cannot be a symlink.")
    if destination.is_file() and destination.stat().st_size:
        return True
    if destination.exists():
        raise ThumbnailGenerationError("Thumbnail output path is invalid.")
    return False


def run_thumbnail_command(command):
    """Translate FFmpeg conversion errors without exposing command details."""
    try:
        execute_ffmpeg(command)
    except FFmpegUnavailableError:
        raise
    except FileNotFoundError as error:
        raise FFmpegUnavailableError(
            "FFmpeg executable is unavailable."
        ) from error
    except subprocess.CalledProcessError as error:
        raise ThumbnailGenerationError(
            "FFmpeg could not generate the video thumbnail."
        ) from error


def validate_thumbnail_file(thumbnail_path):
    """Require FFmpeg to create a non-empty regular image file."""
    if not thumbnail_path.is_file() or thumbnail_path.stat().st_size == 0:
        raise ThumbnailGenerationError("FFmpeg produced no thumbnail image.")


def thumbnail_name(thumbnail_path):
    """Return a portable storage name relative to MEDIA_ROOT."""
    return thumbnail_path.relative_to(settings.MEDIA_ROOT.resolve()).as_posix()
