"""Validation helpers for video metadata and uploaded media files."""

from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError

VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".webm"}
THUMBNAIL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
EBML_HEADER = b"\x1a\x45\xdf\xa3"


def validate_video_upload(upload):
    """Validate video filename, size, and container signature."""
    return validate_upload(upload, VIDEO_EXTENSIONS, "video")


def validate_thumbnail_upload(upload):
    """Validate thumbnail filename, size, and image signature."""
    return validate_upload(upload, THUMBNAIL_EXTENSIONS, "thumbnail")


def validate_upload(upload, allowed_extensions, media_type):
    """Reject unsafe names, oversized files, and mismatched file contents."""
    if not upload or getattr(upload, "_committed", False):
        return upload
    validate_file_name(upload.name)
    extension = Path(upload.name).suffix.lower()
    validate_file_extension(extension, allowed_extensions)
    validate_file_size(upload.size)
    header = read_upload_header(upload)
    if not has_valid_signature(extension, header):
        raise ValidationError(f"Invalid {media_type} file contents.")
    return upload


def validate_file_name(file_name):
    """Reject blank, path-like, and null-byte filenames."""
    unsafe = not file_name or any(char in file_name for char in "/\\\x00")
    if unsafe or file_name in {".", ".."}:
        raise ValidationError("Unsafe uploaded filename.")


def validate_file_extension(extension, allowed_extensions):
    """Require a supported lowercase file extension."""
    if extension not in allowed_extensions:
        raise ValidationError("This file type is not allowed.")


def validate_file_size(file_size):
    """Reject empty files and files above the configured size limit."""
    if file_size <= 0 or file_size > settings.VIDEO_MAX_UPLOAD_BYTES:
        raise ValidationError("This file has an invalid size.")


def read_upload_header(upload):
    """Read a bounded signature prefix without changing file position."""
    position = upload.tell()
    header = upload.read(4096)
    upload.seek(position)
    return header


def has_valid_signature(extension, header):
    """Match allowed extensions to common video or image signatures."""
    if extension in {".mp4", ".m4v", ".mov"}:
        return header[4:8] == b"ftyp"
    if extension == ".mkv":
        return header.startswith(EBML_HEADER) and b"matroska" in header.lower()
    if extension == ".webm":
        return header.startswith(EBML_HEADER) and b"webm" in header.lower()
    return has_image_signature(extension, header)


def has_image_signature(extension, header):
    """Match supported thumbnail extensions to image file headers."""
    if extension in {".jpg", ".jpeg"}:
        return header.startswith(b"\xff\xd8\xff")
    if extension == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    return (
        extension == ".webp"
        and header.startswith(b"RIFF")
        and header[8:12] == b"WEBP"
    )


def validate_plain_text(value):
    """Reject markup delimiters in fields rendered through frontend HTML."""
    if any(character in value for character in "<>\x00"):
        raise ValidationError("HTML markup is not allowed in this field.")
    return value.strip()
