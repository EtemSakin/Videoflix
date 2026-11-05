"""Tests for email-based users and Django Admin integration."""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.admin import EmailUserCreationForm


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
        """Reject superusers missing required staff privileges."""
        with self.assertRaisesMessage(ValueError, "is_staff=True"):
            User.objects.create_superuser(
                "admin@example.com", "StrongPass!73", is_staff=False
            )


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
