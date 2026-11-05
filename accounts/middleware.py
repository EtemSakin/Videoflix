"""Reject unsafe API requests from untrusted browser origins."""

from django.conf import settings
from django.http import JsonResponse


class TrustedOriginMiddleware:
    """Require a configured Origin for state-changing API requests."""

    def __init__(self, get_response):
        """Store the next middleware or view in the request chain."""
        self.get_response = get_response

    def __call__(self, request):
        """Reject untrusted API writes before invoking the endpoint."""
        if self._is_untrusted_api_write(request):
            return JsonResponse({"detail": "Origin not allowed."}, status=403)
        return self.get_response(request)

    @staticmethod
    def _is_untrusted_api_write(request):
        """Identify API writes whose Origin is absent or not configured."""
        allowed_origins = settings.CORS_ALLOWED_ORIGINS
        return (
            request.path_info.startswith("/api/")
            and request.method in ("POST", "PUT", "PATCH", "DELETE")
            and request.headers.get("Origin") not in allowed_origins
        )
