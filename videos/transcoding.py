"""Build safe FFmpeg commands for the supported HLS renditions."""

import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NamedTuple


class HLSProfile(NamedTuple):
    """Describe the video encoding limits for one HLS rendition."""

    height: int
    video_bitrate: str
    max_bitrate: str
    buffer_size: str


HLS_PROFILES = {
    "480p": HLSProfile(480, "1200k", "1400k", "2400k"),
    "720p": HLSProfile(720, "2800k", "3200k", "5600k"),
    "1080p": HLSProfile(1080, "5000k", "5500k", "10000k"),
}


def get_hls_profile(resolution):
    """Return a supported profile or reject untrusted resolutions."""
    try:
        return HLS_PROFILES[resolution]
    except KeyError as error:
        raise ValueError("Unsupported HLS resolution.") from error


class FFmpegUnavailableError(RuntimeError):
    """Report that the FFmpeg executable is unavailable."""


class HLSConversionError(RuntimeError):
    """Report a failed or incomplete HLS conversion."""


def build_hls_command(input_path, output_directory, resolution):
    """Build FFmpeg argv for one HLS rendition without invoking a shell."""
    profile = get_hls_profile(resolution)
    source = str(Path(input_path).resolve())
    destination = Path(output_directory).resolve()
    return [
        "ffmpeg", "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        source,
        *video_arguments(profile),
        *hls_arguments(destination),
    ]


def transcode_hls_rendition(input_path, output_root, resolution):
    """Create one complete HLS rendition and atomically publish its files."""
    get_hls_profile(resolution)
    source = validate_source_file(input_path)
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / resolution
    reject_existing_output(destination)
    with TemporaryDirectory(prefix=".videoflix-", dir=root) as temporary:
        staged = Path(temporary)
        run_ffmpeg(build_hls_command(source, staged, resolution))
        validate_hls_artifacts(staged)
        os.replace(staged, destination)
    return destination


def validate_source_file(input_path):
    """Require a non-empty regular video source file."""
    source = Path(input_path).resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise FileNotFoundError("Video source file is missing or empty.")
    return source


def reject_existing_output(destination):
    """Avoid replacing previously published rendition files implicitly."""
    if destination.exists() or destination.is_symlink():
        raise HLSConversionError("HLS output already exists.")


def run_ffmpeg(command):
    """Run FFmpeg as an argv list and translate expected process failures."""
    try:
        execute_ffmpeg(command)
    except FileNotFoundError as error:
        raise FFmpegUnavailableError(
            "FFmpeg executable is unavailable."
        ) from error
    except subprocess.CalledProcessError as error:
        raise HLSConversionError(
            "FFmpeg could not convert the video."
        ) from error


def execute_ffmpeg(command):
    """Execute argv without a shell and discard potentially sensitive logs."""
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )


def validate_hls_artifacts(output_directory):
    """Require a non-empty manifest and at least one generated segment."""
    manifest = output_directory / "index.m3u8"
    segments = tuple(output_directory.glob("*.ts"))
    if not manifest.is_file() or manifest.stat().st_size == 0 or not segments:
        raise HLSConversionError("FFmpeg produced incomplete HLS output.")
    if any(segment.stat().st_size == 0 for segment in segments):
        raise HLSConversionError("FFmpeg produced an empty HLS segment.")


def video_arguments(profile):
    """Return codec and bounded-size scaling arguments for a rendition."""
    return [
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"scale=-2:{profile.height}",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", profile.video_bitrate,
        "-maxrate", profile.max_bitrate,
        "-bufsize", profile.buffer_size,
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-sn", "-dn", "-map_metadata", "-1",
    ]


def hls_arguments(output_directory):
    """Return deterministic VOD playlist and segment output arguments."""
    return [
        "-f", "hls", "-hls_time", "6", "-hls_playlist_type", "vod",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename",
        str(output_directory / "segment_%05d.ts"),
        str(output_directory / "index.m3u8"),
    ]
