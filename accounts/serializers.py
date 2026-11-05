"""Serializers for account registration."""

from django.contrib.auth import (
    authenticate,
    get_user_model,
    password_validation,
)
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

User = get_user_model()
LOGIN_ERROR = "Invalid email or password."


class RegistrationSerializer(serializers.Serializer):
    """Validate and create inactive users from registration data."""

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    confirmed_password = serializers.CharField(
        write_only=True, trim_whitespace=False
    )
    privacy_policy = serializers.BooleanField(required=False, write_only=True)

    def to_internal_value(self, data):
        """Reject undocumented fields and allow the frontend consent field."""
        if hasattr(data, "keys"):
            unknown_fields = set(data.keys()) - set(self.fields)
            if unknown_fields:
                raise serializers.ValidationError(
                    {"detail": "Unexpected fields are not allowed."}
                )
        return super().to_internal_value(data)

    def validate_email(self, value):
        """Normalize email and reject case-insensitive duplicates."""
        email = value.strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError(
                "Registration could not be completed."
            )
        return email

    def validate(self, attrs):
        """Check password confirmation and configured Django validators."""
        if attrs["password"] != attrs["confirmed_password"]:
            raise serializers.ValidationError({
                "confirmed_password": "Passwords do not match."
            })
        self._validate_django_password(attrs)
        return attrs

    def validate_privacy_policy(self, value):
        """Accept the optional frontend checkbox only when it is checked."""
        if not value:
            raise serializers.ValidationError("Consent must be checked.")
        return value

    def _validate_django_password(self, attrs):
        """Apply Django's password validators to the normalized email user."""
        candidate = User(email=attrs["email"])
        try:
            password_validation.validate_password(
                attrs["password"], user=candidate
            )
        except DjangoValidationError as error:
            raise serializers.ValidationError(
                {"password": error.messages}
            ) from error

    def create(self, validated_data):
        """Create an inactive user with a Django password hash."""
        validated_data.pop("confirmed_password")
        validated_data.pop("privacy_policy", None)
        password = validated_data.pop("password")
        return User.objects.create_user(
            password=password, is_active=False, **validated_data
        )


class LoginSerializer(serializers.Serializer):
    """Authenticate an active user with an email address and password."""

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def to_internal_value(self, data):
        """Reject fields outside the documented login request."""
        if hasattr(data, "keys") and set(data.keys()) - set(self.fields):
            raise serializers.ValidationError(
                {"detail": "Unexpected fields are not allowed."}
            )
        return super().to_internal_value(data)

    def validate_email(self, value):
        """Normalize email casing to match stored user addresses."""
        return value.strip().lower()

    def validate(self, attrs):
        """Authenticate credentials without distinguishing account failures."""
        user = authenticate(
            request=self.context.get("request"),
            email=attrs["email"],
            password=attrs["password"],
        )
        if user is None:
            raise serializers.ValidationError({"detail": LOGIN_ERROR})
        return {"user": user}
