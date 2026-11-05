"""API views for account endpoints."""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.db import IntegrityError, transaction
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)
from rest_framework_simplejwt.tokens import (
    AccessToken,
    RefreshToken,
    UntypedToken,
)

from accounts.emails import (
    EmailDeliveryError,
    send_activation_email,
    send_password_reset_email,
)
from accounts.serializers import (
    LOGIN_ERROR,
    LoginSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RegistrationSerializer,
)

EMAIL_UNIQUE_CONSTRAINTS = {
    "accounts_user_email_ci_unique",
    "accounts_user_email_key",
}
REGISTRATION_ERROR = {"detail": "Registration could not be completed."}
LOGIN_ERROR_RESPONSE = {"detail": LOGIN_ERROR}
ACTIVATION_ERROR = {"message": "Invalid or expired activation link."}
ACTIVATION_SUCCESS = {"message": "Account successfully activated."}
User = get_user_model()
logger = logging.getLogger(__name__)


class RegistrationAPIView(APIView):
    """Register an inactive user and send an activation email."""

    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request):
        """Validate the request and return a safe registration result."""
        serializer = RegistrationSerializer(data=request.data)
        if not serializer.is_valid():
            return registration_error_response()
        try:
            user = create_registered_user(serializer)
        except IntegrityError as error:
            return handle_integrity_error(error)
        except EmailDeliveryError:
            return email_error_response()
        return registration_success_response(user)


class LoginAPIView(APIView):
    """Authenticate users and issue HttpOnly JWT cookies."""

    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request):
        """Validate credentials and return the documented login response."""
        serializer = LoginSerializer(
            data=request.data, context={"request": request}
        )
        if not serializer.is_valid():
            return Response(
                LOGIN_ERROR_RESPONSE, status=status.HTTP_400_BAD_REQUEST
            )
        return login_success_response(serializer.validated_data["user"])


class TokenRefreshAPIView(APIView):
    """Rotate a refresh cookie and issue a new access-token cookie."""

    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request):
        """Refresh tokens without returning bearer tokens in JSON."""
        raw_refresh = request.COOKIES.get(settings.JWT_REFRESH_COOKIE_NAME)
        if not raw_refresh:
            return refresh_error_response("Refresh token is required.", 400)
        tokens = safely_rotate_refresh_token(raw_refresh)
        if tokens is None:
            return refresh_error_response("Invalid refresh token.", 401)
        return refresh_success_response(tokens)


class LogoutAPIView(APIView):
    """Blacklist the refresh token and clear both authentication cookies."""

    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request):
        """Log out the browser and invalidate its refresh token."""
        raw_refresh = request.COOKIES.get(settings.JWT_REFRESH_COOKIE_NAME)
        if not raw_refresh:
            return logout_response("Refresh token is required.", 400)
        if not safely_blacklist_refresh_token(raw_refresh):
            return logout_response("Invalid refresh token.", 401)
        return logout_response("Logout successful", status.HTTP_200_OK)


class PasswordResetRequestAPIView(APIView):
    """Request a password-reset email without exposing account existence."""

    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request):
        """Validate an email and return the generic documented response."""
        serializer = PasswordResetRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                serializer.errors, status=status.HTTP_400_BAD_REQUEST
            )
        request_password_reset(serializer.validated_data["email"])
        return Response(
            {"detail": "An email has been sent to reset your password."},
            status=status.HTTP_200_OK,
        )


class PasswordResetConfirmAPIView(APIView):
    """Confirm a reset link and replace the account password once."""

    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request, uidb64, token):
        """Validate the one-time link and set the new password."""
        uid = decode_activation_uid(uidb64)
        if uid is None:
            return invalid_password_reset_response()
        completed, errors = process_password_reset(uid, token, request.data)
        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)
        if not completed:
            return invalid_password_reset_response()
        return Response(
            {"detail": "Your Password has been successfully reset."},
            status=status.HTTP_200_OK,
        )


class AccountActivationAPIView(APIView):
    """Activate an inactive account through its emailed one-time link."""

    authentication_classes = ()
    permission_classes = (AllowAny,)

    def get(self, request, uidb64, token):
        """Validate the activation link and activate its account once."""
        uid = decode_activation_uid(uidb64)
        if uid is None or not activate_user(uid, token):
            return Response(
                ACTIVATION_ERROR, status=status.HTTP_400_BAD_REQUEST
            )
        return Response(ACTIVATION_SUCCESS, status=status.HTTP_200_OK)


def decode_activation_uid(uidb64):
    """Decode an activation UID, returning None when it is malformed."""
    try:
        return force_str(urlsafe_base64_decode(uidb64))
    except (TypeError, ValueError, OverflowError):
        return None


def activate_user(uid, token):
    """Lock, validate, and activate a user in one database transaction."""
    try:
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=uid)
            if user.is_active or not default_token_generator.check_token(
                user, token
            ):
                return False
            user.is_active = True
            user.save(update_fields=("is_active",))
    except (User.DoesNotExist, TypeError, ValueError, OverflowError):
        return False
    return True


def login_success_response(user):
    """Return the login contract and set both JWT cookies."""
    refresh = RefreshToken.for_user(user)
    response = Response(
        {
            "detail": "Login successful",
            "user": {"id": user.pk, "username": user.email},
        },
        status=status.HTTP_200_OK,
    )
    response["Cache-Control"] = "no-store"
    set_auth_cookies(response, refresh)
    return response


def set_auth_cookies(response, refresh):
    """Set access and refresh tokens with their configured lifetimes."""
    access = refresh.access_token
    set_auth_cookie(response, settings.JWT_ACCESS_COOKIE_NAME, access)
    set_auth_cookie(response, settings.JWT_REFRESH_COOKIE_NAME, refresh)


def set_auth_cookie(response, name, token):
    """Set one HttpOnly JWT cookie using the project's cookie settings."""
    response.set_cookie(
        name,
        str(token),
        max_age=int(token.lifetime.total_seconds()),
        httponly=settings.JWT_COOKIE_HTTPONLY,
        secure=settings.JWT_COOKIE_SECURE,
        samesite=settings.JWT_COOKIE_SAMESITE,
        path=settings.JWT_COOKIE_PATH,
    )


def rotate_refresh_token(raw_refresh):
    """Lock and rotate an unblacklisted refresh token once."""
    with transaction.atomic():
        token = UntypedToken(raw_refresh)
        outstanding = OutstandingToken.objects.select_for_update().get(
            jti=token["jti"]
        )
        if BlacklistedToken.objects.filter(token=outstanding).exists():
            raise TokenError("Refresh token has already been used.")
        serializer = TokenRefreshSerializer(data={"refresh": raw_refresh})
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data


def safely_rotate_refresh_token(raw_refresh):
    """Return no token data for invalid, expired, or inactive refreshes."""
    try:
        return rotate_refresh_token(raw_refresh)
    except (
        TokenError,
        AuthenticationFailed,
        User.DoesNotExist,
        OutstandingToken.DoesNotExist,
    ):
        return None


def refresh_error_response(detail, status_code):
    """Return a non-cacheable refresh error without exposing token data."""
    response = Response({"detail": detail}, status=status_code)
    response["Cache-Control"] = "no-store"
    return response


def refresh_success_response(tokens):
    """Set rotated token cookies and return the documented safe response."""
    response = Response(
        {"detail": "Token refreshed"}, status=status.HTTP_200_OK
    )
    response["Cache-Control"] = "no-store"
    set_refreshed_cookies(response, tokens)
    return response


def set_refreshed_cookies(response, tokens):
    """Set the new access cookie and any rotated refresh cookie."""
    set_auth_cookie(
        response,
        settings.JWT_ACCESS_COOKIE_NAME,
        AccessToken(tokens["access"]),
    )
    if "refresh" in tokens:
        set_auth_cookie(
            response,
            settings.JWT_REFRESH_COOKIE_NAME,
            RefreshToken(tokens["refresh"]),
        )


def safely_blacklist_refresh_token(raw_refresh):
    """Return false for invalid, expired, or already-used tokens."""
    try:
        blacklist_refresh_token(raw_refresh)
    except (TokenError, OutstandingToken.DoesNotExist):
        return False
    return True


def blacklist_refresh_token(raw_refresh):
    """Lock and blacklist a valid refresh token exactly once."""
    with transaction.atomic():
        token = UntypedToken(raw_refresh)
        outstanding = OutstandingToken.objects.select_for_update().get(
            jti=token["jti"]
        )
        if BlacklistedToken.objects.filter(token=outstanding).exists():
            raise TokenError("Refresh token has already been used.")
        RefreshToken(raw_refresh).blacklist()


def logout_response(detail, status_code):
    """Return a private logout response and expire both auth cookies."""
    response = Response({"detail": detail}, status=status_code)
    response["Cache-Control"] = "no-store"
    clear_auth_cookies(response)
    return response


def clear_auth_cookies(response):
    """Expire both configured JWT cookies using their original attributes."""
    clear_auth_cookie(response, settings.JWT_ACCESS_COOKIE_NAME)
    clear_auth_cookie(response, settings.JWT_REFRESH_COOKIE_NAME)


def clear_auth_cookie(response, name):
    """Expire one JWT cookie with the same scope and security flags."""
    response.set_cookie(
        name,
        value="",
        max_age=0,
        expires="Thu, 01 Jan 1970 00:00:00 GMT",
        path=settings.JWT_COOKIE_PATH,
        secure=settings.JWT_COOKIE_SECURE,
        httponly=settings.JWT_COOKIE_HTTPONLY,
        samesite=settings.JWT_COOKIE_SAMESITE,
    )


def request_password_reset(email):
    """Send reset mail without exposing delivery failures."""
    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if user is None:
        return
    try:
        send_password_reset_email(user)
    except EmailDeliveryError:
        logger.error("Password reset email delivery failed.")


def process_password_reset(uid, token, data):
    """Convert invalid reset users and tokens into a controlled failure."""
    try:
        return apply_password_reset(uid, token, data)
    except (User.DoesNotExist, TypeError, ValueError, OverflowError):
        return None, None


def apply_password_reset(uid, token, data):
    """Lock the account, validate the token, and change its password."""
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=uid)
        if not user.is_active or not default_token_generator.check_token(
            user, token
        ):
            return None, None
        errors = validate_and_set_password(user, data)
        if errors:
            return None, errors
    return True, None


def validate_and_set_password(user, data):
    """Validate the new password and save only its Django hash."""
    serializer = PasswordResetConfirmSerializer(
        data=data, context={"user": user}
    )
    if not serializer.is_valid():
        return serializer.errors
    user.set_password(serializer.validated_data["new_password"])
    user.save(update_fields=("password",))
    return None


def invalid_password_reset_response():
    """Return a generic response for invalid or expired reset links."""
    return Response(
        {"detail": "Invalid or expired reset link."},
        status=status.HTTP_400_BAD_REQUEST,
    )


def create_registered_user(serializer):
    """Create the user and deliver its activation email atomically."""
    with transaction.atomic():
        user = serializer.save()
        send_activation_email(user)
    return user


def is_email_conflict(error):
    """Identify only known email uniqueness violations from PostgreSQL."""
    database_error = error.__cause__
    diagnostics = getattr(database_error, "diag", None)
    constraint = getattr(diagnostics, "constraint_name", None)
    return constraint in EMAIL_UNIQUE_CONSTRAINTS


def handle_integrity_error(error):
    """Convert known duplicate-email races and re-raise other DB failures."""
    if not is_email_conflict(error):
        raise error
    return registration_error_response()


def registration_error_response():
    """Return a generic validation response without reflecting user data."""
    return Response(REGISTRATION_ERROR, status=status.HTTP_400_BAD_REQUEST)


def email_error_response():
    """Return a controlled service error when mail delivery fails."""
    detail = {"detail": "Registration is temporarily unavailable."}
    return Response(detail, status=status.HTTP_503_SERVICE_UNAVAILABLE)


def registration_success_response(user):
    """Return user fields without exposing the activation token."""
    result = {"user": {"id": user.pk, "email": user.email}}
    return Response(result, status=status.HTTP_201_CREATED)
