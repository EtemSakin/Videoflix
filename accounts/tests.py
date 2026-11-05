"""Tests for email-based users and Django Admin integration."""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.admin import EmailUserCreationForm
from accounts.serializers import RegistrationSerializer


User = get_user_model()


class UserManagerTests(TestCase):
    """Verify email-based user and superuser creation."""

    def test_create_user_normalizes_email_and_hashes_password(self):
        """Normalize the email and hash the supplied password."""
        user = User.objects.create_user(
            "  FilmFan@Example.com  ", "StrongPass!73"
        )

        self.assertEqual(user.email, "filmfan@example.com")
        self.assertTrue(user.check_password("StrongPass!73"))
        self.assertNotEqual(user.password, "StrongPass!73")

    def test_email_is_the_unique_login_identifier(self):
        """Use email for authentication without a username database field."""
        self.assertEqual(User.USERNAME_FIELD, "email")
        field_names = {field.name for field in User._meta.fields}
        self.assertNotIn("username", field_names)
        user = User.objects.create_user("viewer@example.com", "StrongPass!73")
        self.assertEqual(user.get_username(), user.email)

    def test_email_uniqueness_ignores_case(self):
        """Reject case-only duplicates through the database constraint."""
        User.objects.create(email="Viewer@Example.com")

        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create(email="viewer@example.com")

    def test_create_superuser_sets_required_privileges(self):
        """Give created superusers active staff and superuser flags."""
        user = User.objects.create_superuser(
            "admin@example.com", "StrongPass!73"
        )

        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_active)

    def test_create_superuser_rejects_missing_privileges(self):
        """Reject superusers missing any required privileged flag."""
        invalid_flags = (
            ("is_staff", False, "is_staff=True"),
            ("is_superuser", False, "is_superuser=True"),
            ("is_active", False, "is_active=True"),
        )
        for field, value, message in invalid_flags:
            with self.subTest(field=field), self.assertRaisesMessage(
                ValueError, message
            ):
                User.objects.create_superuser(
                    "admin@example.com", "StrongPass!73", **{field: value}
                )

    def test_create_user_rejects_missing_email(self):
        """Reject absent and whitespace-only manager email input."""
        for email in (None, "", "   "):
            with self.subTest(email=email), self.assertRaisesMessage(
                ValueError, "An email address is required."
            ):
                User.objects.create_user(email, "StrongPass!73")


class UserAdminTests(TestCase):
    """Verify the admin works without a username field."""

    def test_user_admin_is_registered_with_email_fields(self):
        """Register the user model with email-based admin fields."""
        model_admin = admin.site._registry[User]

        self.assertIsInstance(model_admin, DjangoUserAdmin)
        self.assertEqual(model_admin.ordering, ("email",))
        self.assertEqual(model_admin.search_fields, ("email",))

    def test_admin_creation_form_creates_email_user(self):
        """Create a user from the email-only admin form."""
        form = EmailUserCreationForm(
            data={
                "email": "AdminUser@Example.com",
                "password1": "StrongPass!73",
                "password2": "StrongPass!73",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertEqual(user.email, "adminuser@example.com")
        self.assertTrue(user.check_password("StrongPass!73"))


class RegistrationSerializerTests(TestCase):
    """Verify registration validation and inactive account creation."""

    def valid_payload(self, **overrides):
        """Return a valid registration payload with optional overrides."""
        payload = {
            "email": "FilmFan@example.com",
            "password": "StrongPass!73",
            "confirmed_password": "StrongPass!73",
        }
        payload.update(overrides)
        return payload

    def test_valid_registration_creates_inactive_hashed_user(self):
        """Create an inactive account without storing plaintext password."""
        serializer = RegistrationSerializer(data=self.valid_payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)
        user = serializer.save()
        self.assertFalse(user.is_active)
        self.assertTrue(user.check_password("StrongPass!73"))
        self.assertNotEqual(user.password, "StrongPass!73")

    def test_registration_normalizes_email_case(self):
        """Normalize the email address before persisting the user."""
        serializer = RegistrationSerializer(data=self.valid_payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.save().email, "filmfan@example.com")

    def test_password_fields_are_write_only(self):
        """Exclude both submitted password fields from serializer output."""
        serializer = RegistrationSerializer(data=self.valid_payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        self.assertEqual(set(serializer.data), {"email"})

    def test_password_confirmation_must_match(self):
        """Reject registration when the two passwords differ."""
        payload = self.valid_payload(confirmed_password="DifferentPass!73")
        serializer = RegistrationSerializer(data=payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn("confirmed_password", serializer.errors)

    def test_missing_required_fields_are_rejected(self):
        """Require email, password, and password confirmation."""
        for field in ("email", "password", "confirmed_password"):
            payload = self.valid_payload()
            del payload[field]
            self.assertFalse(RegistrationSerializer(data=payload).is_valid())

    def test_invalid_email_is_rejected(self):
        """Reject malformed email addresses."""
        serializer = RegistrationSerializer(
            data=self.valid_payload(email="not-an-email")
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("email", serializer.errors)

    def test_weak_passwords_are_rejected(self):
        """Apply Django's length, common, and numeric password validators."""
        for password in ("short", "password", "12345678"):
            serializer = RegistrationSerializer(
                data=self.valid_payload(
                    password=password, confirmed_password=password
                )
            )
            self.assertFalse(serializer.is_valid())
            self.assertIn("password", serializer.errors)

    def test_password_similar_to_email_is_rejected(self):
        """Apply Django's user-attribute similarity password validator."""
        password = "filmfan@example.com"
        serializer = RegistrationSerializer(
            data=self.valid_payload(
                password=password, confirmed_password=password
            )
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("password", serializer.errors)

    def test_duplicate_email_is_rejected_without_case_sensitivity(self):
        """Reject duplicate registrations irrespective of email letter case."""
        User.objects.create_user("filmfan@example.com", "StrongPass!73")
        serializer = RegistrationSerializer(
            data=self.valid_payload(email="FILMFAN@EXAMPLE.COM")
        )
        self.assertFalse(serializer.is_valid())
        self.assertEqual(
            str(serializer.errors["email"][0]),
            "Registration could not be completed.",
        )
