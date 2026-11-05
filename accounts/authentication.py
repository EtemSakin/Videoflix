"""DRF authentication using access JWTs from HttpOnly cookies."""

from django.conf import settings
from rest_framework_simplejwt.authentication import JWTAuthentication


class CookieJWTAuthentication(JWTAuthentication):
    """Authenticate requests with the configured access-token cookie."""

    def authenticate(self, request):
        """Validate a cookie token and return its active user and claims."""
        raw_token = request.COOKIES.get(settings.JWT_ACCESS_COOKIE_NAME)
        if raw_token is None:
            return None
        validated_token = self.get_validated_token(raw_token.encode("utf-8"))
        return self.get_user(validated_token), validated_token
