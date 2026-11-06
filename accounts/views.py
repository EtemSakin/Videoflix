"""API views for account endpoints."""

from django.conf import settings
from django.db import IntegrityError
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.emails import EmailDeliveryError
from accounts.serializers import (
    LoginSerializer,
    PasswordResetRequestSerializer,
    RegistrationSerializer,
)
from accounts.utils import (
    ACTIVATION_ERROR,
    ACTIVATION_SUCCESS,
    LOGIN_ERROR_RESPONSE,
    activate_user,
    create_registered_user,
    decode_activation_uid,
    email_error_response,
    handle_integrity_error,
    invalid_password_reset_response,
    login_success_response,
    logout_response,
    process_password_reset,
    refresh_error_response,
    refresh_success_response,
    registration_error_response,
    registration_success_response,
    request_password_reset,
    safely_blacklist_refresh_token,
    safely_rotate_refresh_token,
)


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
