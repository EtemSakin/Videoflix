"""Serializers for account registration."""

from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers


User = get_user_model()


class RegistrationSerializer(serializers.Serializer):
    """Validate and create inactive users from registration data."""

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    confirmed_password = serializers.CharField(
        write_only=True, trim_whitespace=False
    )

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
        password = validated_data.pop("password")
        return User.objects.create_user(
            password=password, is_active=False, **validated_data
        )
