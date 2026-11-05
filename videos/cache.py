"""Redis cache helpers for the video catalog."""

from django.core.cache import cache
from redis.exceptions import RedisError

from videos.models import Video
from videos.serializers import VideoSerializer

VIDEO_CATALOG_CACHE_KEY = "videoflix:video_catalog:v1"
VIDEO_CATALOG_CACHE_TIMEOUT = 300


def get_video_catalog_data():
    """Return cached serialized catalog data or build it from PostgreSQL."""
    try:
        cached_data = cache.get(VIDEO_CATALOG_CACHE_KEY)
    except RedisError:
        cached_data = None
    if cached_data is not None:
        return cached_data
    return build_and_cache_video_catalog()


def build_and_cache_video_catalog():
    """Serialize ready videos and cache only primitive response values."""
    videos = Video.objects.filter(status=Video.STATUS_READY).order_by(
        "-created_at", "id"
    )
    data = [dict(item) for item in VideoSerializer(videos, many=True).data]

    cache_catalog_data(data)
    return data


def cache_catalog_data(data):
    """Store serialized catalog values when Redis is available."""
    try:
        cache.set(
            VIDEO_CATALOG_CACHE_KEY,
            data,
            timeout=VIDEO_CATALOG_CACHE_TIMEOUT,
        )
    except RedisError:
        pass


def absolute_thumbnail_urls(catalog, request):
    """Build host-specific thumbnail URLs without caching request hosts."""
    response_data = []
    for video in catalog:
        item = video.copy()
        if item["thumbnail_url"]:
            item["thumbnail_url"] = request.build_absolute_uri(
                item["thumbnail_url"]
            )
        response_data.append(item)
    return response_data


def invalidate_video_catalog_cache():
    """Remove cached catalog data while tolerating temporary Redis outages."""
    try:
        cache.delete(VIDEO_CATALOG_CACHE_KEY)
    except RedisError:
        return
