"""API views for account endpoints."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.db import IntegrityError, transaction
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.emails import EmailDeliveryError, send_activation_email
from accounts.serializers import (
    LOGIN_ERROR,
    LoginSerializer,
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
