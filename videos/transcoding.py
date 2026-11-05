"""Build safe FFmpeg commands for the supported HLS renditions."""

from pathlib import Path
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


def video_arguments(profile):
    """Return codec and bounded-size scaling arguments for a rendition."""
    return [
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"scale=-2:min(ih\\,{profile.height})",
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
