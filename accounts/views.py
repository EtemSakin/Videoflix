"""API views for account endpoints."""

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.emails import EmailDeliveryError, send_activation_email
from accounts.serializers import RegistrationSerializer

EMAIL_UNIQUE_CONSTRAINTS = {
    "accounts_user_email_ci_unique",
    "accounts_user_email_key",
}
REGISTRATION_ERROR = {"detail": "Registration could not be completed."}


class RegistrationAPIView(APIView):
    """Register an inactive user and send an activation email."""

    permission_classes = [AllowAny]

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
