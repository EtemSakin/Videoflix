"""Tests for email-based users and Django Admin integration."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from smtplib import SMTPException
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.db import (
    IntegrityError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIClient, APIRequestFactory, APITestCase
from rest_framework.views import APIView
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from accounts.admin import EmailUserCreationForm
from accounts.authentication import CookieJWTAuthentication
from accounts.emails import build_activation_url
from accounts.serializers import RegistrationSerializer

User = get_user_model()


class ProtectedCookieAPIView(APIView):
    """Provide an isolated protected view for authentication tests."""

    permission_classes = (IsAuthenticated,)

    def get(self, request):
        """Return the authenticated user ID for test assertions."""
        return Response({"user_id": request.user.pk})


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


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="Videoflix <noreply@example.com>",
    FRONTEND_BASE_URL="http://127.0.0.1:5500",
)
class RegistrationEndpointTests(APITestCase):
    """Test registration API behavior and activation email delivery."""

    def setUp(self):
        """Prepare the route and a valid registration payload."""
        self.client.defaults["HTTP_ORIGIN"] = "http://127.0.0.1:5500"
        self.url = reverse("register")
        self.payload = {
            "email": "FilmFan@example.com",
            "password": "StrongPass!73",
            "confirmed_password": "StrongPass!73",
        }

    def submit_registration(self, payload=None):
        """Post JSON registration data without authenticating the client."""
        data = self.payload if payload is None else payload
        return self.client.post(self.url, data, format="json")

    def test_three_fields_register_an_inactive_user_without_auth(self):
        """Register anonymously with documented fields and hash password."""
        cookie = f"{settings.JWT_ACCESS_COOKIE_NAME}=invalid"
        response = self.client.post(
            self.url, self.payload, format="json", HTTP_COOKIE=cookie
        )
        user = User.objects.get(email="filmfan@example.com")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(user.is_active)
        self.assertTrue(user.check_password("StrongPass!73"))
        self.assertEqual(len(mail.outbox), 1)

    def test_optional_checked_privacy_policy_is_accepted(self):
        """Accept the frontend checkbox without storing it on the user."""
        payload = {**self.payload, "privacy_policy": "on"}
        response = self.submit_registration(payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn("privacy_policy", {f.name for f in User._meta.fields})

    def test_unchecked_privacy_policy_values_are_rejected(self):
        """Reject privacy checkbox values that do not express consent."""
        for value in (False, "false", "off", "not-consent"):
            response = self.submit_registration(
                {**self.payload, "privacy_policy": value}
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_other_unknown_fields_are_rejected(self):
        """Reject fields beyond the documented contract and checkbox."""
        payload = {**self.payload, "username": "fan"}
        self.assertEqual(self.submit_registration(payload).status_code, 400)

    def test_invalid_email_is_rejected(self):
        """Reject malformed registration email through the API."""
        payload = {**self.payload, "email": "not-an-email"}
        self.assertEqual(self.submit_registration(payload).status_code, 400)

    def test_missing_required_fields_are_rejected(self):
        """Reject requests missing any documented required field."""
        for field in ("email", "password", "confirmed_password"):
            payload = self.payload.copy()
            del payload[field]
            response = self.submit_registration(payload)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_mismatched_passwords_are_rejected(self):
        """Reject password confirmation that does not match the password."""
        payload = {**self.payload, "confirmed_password": "DifferentPass!73"}
        self.assertEqual(self.submit_registration(payload).status_code, 400)

    def test_weak_password_is_rejected(self):
        """Reject weak passwords through the API serializer validators."""
        payload = {**self.payload, "password": "password"}
        payload["confirmed_password"] = "password"
        self.assertEqual(self.submit_registration(payload).status_code, 400)

    def test_case_insensitive_duplicate_email_is_rejected(self):
        """Reject duplicate registration and return a generic response."""
        User.objects.create_user("filmfan@example.com", "StrongPass!73")
        payload = {**self.payload, "email": "FILMFAN@example.com"}
        response = self.submit_registration(payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json(),
            {"detail": "Registration could not be completed."},
        )

    def test_activation_email_contains_expected_url_and_valid_token(self):
        """Verify frontend activation parameters and Django token validity."""
        self.submit_registration()
        user = User.objects.get(email="filmfan@example.com")
        link = mail.outbox[0].body.strip().splitlines()[-1]
        query = parse_qs(urlparse(link).query)
        expected_uid = urlsafe_base64_encode(force_bytes(user.pk))
        self.assertEqual(urlparse(link).path, "/pages/auth/activate.html")
        self.assertEqual(query["uid"], [expected_uid])
        self.assertTrue(
            default_token_generator.check_token(user, query["token"][0])
        )

    def test_email_has_html_and_text_alternatives(self):
        """Deliver both plaintext and HTML activation email bodies."""
        self.submit_registration()
        self.assertEqual(len(mail.outbox[0].alternatives), 1)
        self.assertEqual(mail.outbox[0].alternatives[0][1], "text/html")
        self.assertIn("activate.html?uid=", mail.outbox[0].alternatives[0][0])

    def test_response_contains_no_password_or_activation_token(self):
        """Keep credentials and activation tokens out of the API response."""
        response = self.submit_registration()
        link = mail.outbox[0].body.splitlines()[-1]
        token = parse_qs(urlparse(link).query)["token"][0]
        self.assertNotIn("StrongPass!73", response.content.decode())
        self.assertNotIn(token, response.content.decode())
        self.assertEqual(set(response.json()), {"user"})

    def test_email_failure_rolls_back_user_and_returns_service_error(self):
        """Roll back user creation when SMTP delivery fails."""
        send_path = "accounts.emails.EmailMultiAlternatives.send"
        with patch(send_path, side_effect=SMTPException):
            response = self.submit_registration()
        self.assertEqual(
            response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE
        )
        user_exists = User.objects.filter(email="filmfan@example.com").exists()
        self.assertFalse(user_exists)

    def test_concurrent_email_conflict_returns_controlled_error(self):
        """Return a safe response for a concurrent email uniqueness error."""
        cause = Exception("duplicate key")
        cause.diag = SimpleNamespace(
            constraint_name="accounts_user_email_ci_unique"
        )
        error = IntegrityError("duplicate email")
        error.__cause__ = cause
        with patch("accounts.views.create_registered_user", side_effect=error):
            response = self.submit_registration()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class AccountActivationTests(APITestCase):
    """Test the public account activation endpoint."""

    def setUp(self):
        """Create an inactive account and its valid activation route."""
        self.user = User.objects.create_user(
            "activate@example.com", "StrongPass!73", is_active=False
        )
        self.uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        self.token = default_token_generator.make_token(self.user)
        self.url = self.path_for_token(self.token)

    def path_for_token(self, token, uid=None):
        """Build the official activation API path for a token and UID."""
        return reverse(
            "activate", kwargs={"uidb64": uid or self.uid, "token": token}
        )

    def test_anonymous_activation_succeeds_with_exact_response(self):
        """Allow anonymous activation and return the official success body."""
        cookie = f"{settings.JWT_ACCESS_COOKIE_NAME}=invalid"
        response = self.client.get(self.url, HTTP_COOKIE=cookie)
        self.user.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json(), {"message": "Account successfully activated."}
        )
        self.assertTrue(self.user.is_active)

    def test_malformed_uid_returns_generic_bad_request(self):
        """Reject a malformed encoded UID without exposing account details."""
        path = self.path_for_token(self.token, "not-base64!")
        response = self.client.get(path)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json(), {"message": "Invalid or expired activation link."}
        )

    def test_unknown_uid_returns_generic_bad_request(self):
        """Reject a well-formed UID that does not identify an account."""
        uid = urlsafe_base64_encode(force_bytes(999999))
        response = self.client.get(self.path_for_token(self.token, uid))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_token_returns_generic_bad_request(self):
        """Reject a manipulated activation token."""
        response = self.client.get(self.path_for_token("not-a-valid-token"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(PASSWORD_RESET_TIMEOUT=60)
    def test_expired_token_returns_generic_bad_request(self):
        """Reject a token generated outside Django's configured timeout."""
        expired_at = datetime.now() - timedelta(minutes=2)
        with patch.object(
            default_token_generator, "_now", return_value=expired_at
        ):
            token = default_token_generator.make_token(self.user)
        response = self.client.get(self.path_for_token(token))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_already_active_account_is_rejected(self):
        """Reject activation when the account is already active."""
        User.objects.filter(pk=self.user.pk).update(is_active=True)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_activation_link_cannot_be_reused(self):
        """Allow exactly one successful use of an activation link."""
        first_response = self.client.get(self.url)
        second_response = self.client.get(self.url)
        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            second_response.status_code, status.HTTP_400_BAD_REQUEST
        )

    @override_settings(FRONTEND_BASE_URL="http://127.0.0.1:5500")
    def test_commit_five_frontend_link_activates_account(self):
        """Accept UID and token from the registration email frontend link."""
        link = build_activation_url(self.user)
        params = parse_qs(urlparse(link).query)
        path = self.path_for_token(params["token"][0], params["uid"][0])
        response = self.client.get(path)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json(), {"message": "Account successfully activated."}
        )


class ConcurrentAccountActivationTests(TransactionTestCase):
    """Verify row locking across independent PostgreSQL connections."""

    def setUp(self):
        """Create one inactive user and a valid token for both requests."""
        self.user = User.objects.create_user(
            "parallel@example.com", "StrongPass!73", is_active=False
        )
        self.uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        self.token = default_token_generator.make_token(self.user)
        self.url = reverse(
            "activate", kwargs={"uidb64": self.uid, "token": self.token}
        )

    @staticmethod
    def activate_after_barrier(url, barrier):
        """Issue a synchronized activation request on a fresh connection."""
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                backend_pid = cursor.fetchone()[0]
            barrier.wait(timeout=10)
            response = APIClient().get(url)
            return response.status_code, backend_pid
        finally:
            connections.close_all()

    def test_only_one_parallel_activation_succeeds(self):
        """Serialize concurrent activation and allow exactly one success."""
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _: self.activate_after_barrier(self.url, barrier),
                    range(2),
                )
            )
        statuses, backend_pids = zip(*results)
        self.assertCountEqual(statuses, [200, 400])
        self.assertEqual(len(set(backend_pids)), 2)
        self.assertTrue(User.objects.get(pk=self.user.pk).is_active)


class LoginEndpointTests(APITestCase):
    """Verify email login responses and JWT cookie behavior."""

    def setUp(self):
        """Prepare an active account and the official login route."""
        self.client.defaults["HTTP_ORIGIN"] = "http://127.0.0.1:5500"
        self.password = "StrongPass!73"
        self.user = User.objects.create_user(
            "login@example.com", self.password
        )
        self.url = reverse("login")
        self.credentials = {
            "email": self.user.email,
            "password": self.password,
        }

    def test_untrusted_origin_is_rejected_before_login(self):
        """Reject a cross-origin login attempt before credential checks."""
        response = self.client.post(
            self.url,
            self.credentials,
            format="json",
            HTTP_ORIGIN="https://evil.example",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_missing_origin_is_rejected_for_api_writes(self):
        """Require browser Origin metadata on state-changing API requests."""
        self.client.defaults.pop("HTTP_ORIGIN")
        response = self.client.post(self.url, self.credentials, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_trusted_frontend_origin_can_login_without_csrf_header(self):
        """Keep the official credentialed frontend login flow compatible."""
        response = self.submit_login()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response["Access-Control-Allow-Origin"],
            "http://127.0.0.1:5500",
        )
        self.assertEqual(response["Access-Control-Allow-Credentials"], "true")

    def test_preflight_allows_the_configured_frontend_origin(self):
        """Allow the JSON preflight sent before cross-origin frontend posts."""
        response = self.client.options(
            self.url,
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response["Access-Control-Allow-Origin"],
            "http://127.0.0.1:5500",
        )

    def submit_login(self, data=None):
        """Post credentials anonymously to the login endpoint."""
        payload = self.credentials if data is None else data
        return self.client.post(self.url, payload, format="json")

    def test_anonymous_login_returns_contract_and_jwt_cookies(self):
        """Return the documented user object and HttpOnly tokens."""
        response = self.submit_login(
            {**self.credentials, "email": "LOGIN@example.com"}
        )
        expected = {
            "detail": "Login successful",
            "user": {"id": self.user.pk, "username": self.user.email},
        }
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), expected)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertNotIn(self.password, response.content.decode())
        self.assert_login_cookies(response)

    def assert_login_cookies(self, response):
        """Verify cookie attributes, lifetime, and encoded account ID."""
        access = response.cookies[settings.JWT_ACCESS_COOKIE_NAME]
        refresh = response.cookies[settings.JWT_REFRESH_COOKIE_NAME]
        self.assert_cookie_attributes(access)
        self.assert_cookie_attributes(refresh)
        self.assert_cookie_user(access, AccessToken)
        self.assert_cookie_user(refresh, RefreshToken)

    def assert_cookie_attributes(self, cookie):
        """Verify security attributes on a JWT cookie."""
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["path"], settings.JWT_COOKIE_PATH)
        self.assertEqual(cookie["samesite"], settings.JWT_COOKIE_SAMESITE)
        self.assertEqual(bool(cookie["secure"]), settings.JWT_COOKIE_SECURE)
        self.assertGreater(int(cookie["max-age"]), 0)

    def assert_cookie_user(self, cookie, token_class):
        """Check the account ID encoded in an access or refresh cookie."""
        token = token_class(cookie.value)
        self.assertEqual(str(token["user_id"]), str(self.user.pk))

    def assert_generic_login_error(self, response):
        """Assert the same public failure for every invalid credential."""
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json(), {"detail": "Invalid email or password."}
        )

    def test_authentication_failures_are_indistinguishable(self):
        """Hide account existence behind one status and response body."""
        wrong_password = {**self.credentials, "password": "wrong"}
        unknown_email = {**self.credentials, "email": "absent@example.com"}
        responses = [
            self.submit_login(wrong_password), self.submit_login(unknown_email)
        ]
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        responses.append(self.submit_login())
        for response in responses:
            self.assert_generic_login_error(response)

    def test_missing_fields_are_rejected(self):
        """Require both documented login fields."""
        incomplete_payloads = (
            {},
            {"email": self.user.email},
            {"password": self.password},
        )
        for payload in incomplete_payloads:
            self.assertEqual(
                self.submit_login(payload).status_code,
                status.HTTP_400_BAD_REQUEST,
            )

    def test_invalid_email_is_rejected(self):
        """Reject malformed email input without authenticating."""
        response = self.submit_login({**self.credentials, "email": "invalid"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_fields_are_rejected(self):
        """Reject undocumented login fields."""
        payload = {**self.credentials, "username": "ignored"}
        response = self.submit_login(payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_public_login_ignores_invalid_access_cookie(self):
        """Allow login when a stale browser cookie is present."""
        response = self.client.post(
            self.url,
            self.credentials,
            format="json",
            HTTP_COOKIE=f"{settings.JWT_ACCESS_COOKIE_NAME}=invalid",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class CookieJWTAuthenticationTests(APITestCase):
    """Test cookie authentication against a protected DRF view."""

    def setUp(self):
        """Create a user, API request factory, and protected test view."""
        self.user = User.objects.create_user(
            "cookie-auth@example.com", "StrongPass!73"
        )
        self.factory = APIRequestFactory()
        self.view = ProtectedCookieAPIView.as_view()
        token = RefreshToken.for_user(self.user).access_token
        self.access_token = str(token)

    def request_with_cookie(self, value=None, authorization=None):
        """Call the protected view with optional cookie or header input."""
        headers = {}
        if value is not None:
            cookie = settings.JWT_ACCESS_COOKIE_NAME
            headers["HTTP_COOKIE"] = f"{cookie}={value}"
        if authorization is not None:
            headers["HTTP_AUTHORIZATION"] = authorization
        request = self.factory.get("/protected/", **headers)
        return self.view(request)

    def test_cookie_authentication_is_configured_by_default(self):
        """Use cookie JWTs as the global DRF authentication class."""
        self.assertEqual(
            ProtectedCookieAPIView.authentication_classes,
            [CookieJWTAuthentication],
        )

    def test_valid_access_cookie_authenticates_user(self):
        """Resolve the cookie's signed subject to the active database user."""
        response = self.request_with_cookie(self.access_token)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"user_id": self.user.pk})

    def test_missing_cookie_is_unauthorized(self):
        """Reject protected requests without an access-token cookie."""
        response = self.request_with_cookie()
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_invalid_cookie_is_unauthorized(self):
        """Reject forged token values without authenticating the request."""
        response = self.request_with_cookie("forged.token.value")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_refresh_token_is_not_accepted_as_access_cookie(self):
        """Reject refresh tokens presented in the access-cookie slot."""
        refresh = str(RefreshToken.for_user(self.user))
        response = self.request_with_cookie(refresh)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_bearer_header_alone_is_not_authenticated(self):
        """Require a cookie instead of accepting a stray header token."""
        response = self.request_with_cookie(
            authorization=f"Bearer {self.access_token}"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_expired_cookie_is_unauthorized(self):
        """Reject access tokens past their expiration timestamp."""
        token = RefreshToken.for_user(self.user).access_token
        token.set_exp(lifetime=timedelta(seconds=-1))
        response = self.request_with_cookie(str(token))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_inactive_user_is_unauthorized(self):
        """Reject validly signed tokens belonging to inactive users."""
        user = User.objects.create_user(
            "inactive-cookie@example.com", "StrongPass!73"
        )
        token = str(RefreshToken.for_user(user).access_token)
        User.objects.filter(pk=user.pk).update(is_active=False)
        response = self.request_with_cookie(token)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_deleted_user_is_unauthorized(self):
        """Reject valid tokens after their user account has been removed."""
        User.objects.filter(pk=self.user.pk).delete()
        response = self.request_with_cookie(self.access_token)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class TokenRefreshEndpointTests(APITestCase):
    """Verify refresh-cookie rotation and safe endpoint responses."""

    def setUp(self):
        """Create an active user and a refresh token for API tests."""
        self.user = User.objects.create_user(
            "refresh@example.com", "StrongPass!73"
        )
        self.refresh = RefreshToken.for_user(self.user)
        self.raw_refresh = str(self.refresh)
        self.url = reverse("token_refresh")
        self.client.defaults["HTTP_ORIGIN"] = "http://127.0.0.1:5500"

    def submit_refresh(self, token=None):
        """Post with the chosen token in the configured refresh cookie."""
        value = self.raw_refresh if token is None else token
        self.client.cookies[settings.JWT_REFRESH_COOKIE_NAME] = value
        return self.client.post(self.url, {}, format="json")

    def test_valid_refresh_sets_rotated_httponly_cookies(self):
        """Return 200, rotate both cookies, and hide tokens from the body."""
        response = self.submit_refresh()
        access = response.cookies[settings.JWT_ACCESS_COOKIE_NAME]
        refresh = response.cookies[settings.JWT_REFRESH_COOKIE_NAME]
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"detail": "Token refreshed"})
        self.assertTrue(access["httponly"])
        self.assertTrue(refresh["httponly"])
        self.assertNotEqual(refresh.value, self.raw_refresh)
        self.assertTrue(
            BlacklistedToken.objects.filter(token__jti=self.refresh["jti"])
            .exists()
        )

    def test_missing_refresh_cookie_returns_400(self):
        """Return 400 when the request has no refresh cookie."""
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_refresh_cookie_returns_401(self):
        """Reject malformed refresh-cookie values with 401."""
        response = self.submit_refresh("malformed-token")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_access_token_is_not_accepted_as_a_refresh_token(self):
        """Reject access JWTs supplied in the refresh-cookie slot."""
        access = str(self.refresh.access_token)
        response = self.submit_refresh(access)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_expired_refresh_cookie_returns_401(self):
        """Reject expired refresh tokens without issuing cookies."""
        expired = RefreshToken.for_user(self.user)
        expired.set_exp(lifetime=timedelta(seconds=-1))
        response = self.submit_refresh(str(expired))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_inactive_user_cannot_refresh(self):
        """Reject refresh tokens after their account has been disabled."""
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        response = self.submit_refresh()
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_deleted_user_cannot_refresh(self):
        """Handle deleted token owners as invalid instead of a server error."""
        User.objects.filter(pk=self.user.pk).delete()
        response = self.submit_refresh()
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_rotated_refresh_token_cannot_be_replayed(self):
        """Reject a refresh token that has already been rotated."""
        first = self.submit_refresh()
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        response = self.submit_refresh(self.raw_refresh)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class LogoutEndpointTests(APITestCase):
    """Verify logout blacklists refresh tokens and expires auth cookies."""

    def setUp(self):
        """Create an active user and cookies for logout requests."""
        self.user = User.objects.create_user(
            "logout@example.com", "StrongPass!73"
        )
        self.refresh = RefreshToken.for_user(self.user)
        self.raw_refresh = str(self.refresh)
        self.url = reverse("logout")
        self.client.defaults["HTTP_ORIGIN"] = "http://127.0.0.1:5500"
        self.client.cookies[settings.JWT_ACCESS_COOKIE_NAME] = str(
            self.refresh.access_token
        )

    def submit_logout(self, token=None):
        """Post with the chosen refresh cookie and an access cookie."""
        value = self.raw_refresh if token is None else token
        self.client.cookies[settings.JWT_REFRESH_COOKIE_NAME] = value
        return self.client.post(self.url, {}, format="json")

    def assert_auth_cookies_expired(self, response):
        """Check logout expires each cookie with its configured scope."""
        for name in (
            settings.JWT_ACCESS_COOKIE_NAME,
            settings.JWT_REFRESH_COOKIE_NAME,
        ):
            cookie = response.cookies[name]
            self.assertEqual(cookie["max-age"], 0)
            self.assertEqual(cookie["path"], settings.JWT_COOKIE_PATH)
            self.assertTrue(cookie["httponly"])
            self.assertEqual(
                bool(cookie["secure"]), settings.JWT_COOKIE_SECURE
            )
            self.assertEqual(cookie["samesite"], settings.JWT_COOKIE_SAMESITE)

    def test_logout_blacklists_refresh_and_expires_both_cookies(self):
        """Blacklist the supplied token and remove browser auth cookies."""
        response = self.submit_logout()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"detail": "Logout successful"})
        self.assert_auth_cookies_expired(response)
        self.assertTrue(
            BlacklistedToken.objects.filter(token__jti=self.refresh["jti"])
            .exists()
        )

    def test_refresh_token_cannot_be_reused_after_logout(self):
        """Reject the old refresh token after logout."""
        self.submit_logout()
        self.client.cookies[
            settings.JWT_REFRESH_COOKIE_NAME
        ] = self.raw_refresh
        response = self.client.post(
            reverse("token_refresh"),
            {},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_missing_refresh_returns_400_and_expires_cookies(self):
        """Return 400 for a missing refresh cookie and expire auth cookies."""
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assert_auth_cookies_expired(response)

    def test_invalid_refresh_returns_401_and_expires_cookies(self):
        """Clear browser cookies when the supplied refresh token is invalid."""
        response = self.submit_logout("malformed-token")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assert_auth_cookies_expired(response)

    def test_anonymous_logout_does_not_require_access_authentication(self):
        """Allow logout with a refresh cookie and no access cookie."""
        self.client.cookies.pop(settings.JWT_ACCESS_COOKIE_NAME)
        response = self.submit_logout()
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ConcurrentTokenRefreshTests(TransactionTestCase):
    """Verify concurrent requests cannot rotate a token more than once."""

    def setUp(self):
        """Create a user, its outstanding refresh token, and endpoint URL."""
        self.user = User.objects.create_user(
            "parallel-refresh@example.com", "StrongPass!73"
        )
        self.refresh = str(RefreshToken.for_user(self.user))
        self.url = reverse("token_refresh")

    @staticmethod
    def refresh_after_barrier(url, token, barrier):
        """Submit a refresh from a fresh synchronized PostgreSQL connection."""
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                backend_pid = cursor.fetchone()[0]
            barrier.wait(timeout=10)
            response = submit_refresh_request(url, token)
            return response.status_code, backend_pid
        finally:
            connections.close_all()

    def run_parallel_refreshes(self):
        """Run two simultaneous requests using one refresh token."""
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(
                executor.map(
                    lambda _: self.refresh_after_barrier(
                        self.url, self.refresh, barrier
                    ),
                    range(2),
                )
            )

    def test_only_one_parallel_refresh_succeeds(self):
        """Allow only one of two simultaneous uses of the same token."""
        results = self.run_parallel_refreshes()
        statuses, backend_pids = zip(*results)
        self.assertCountEqual(statuses, [200, 401])
        self.assertEqual(len(set(backend_pids)), 2)


def submit_refresh_request(url, token):
    """Send one browser-compatible refresh request with the given cookie."""
    client = APIClient()
    client.cookies[settings.JWT_REFRESH_COOKIE_NAME] = token
    return client.post(
        url,
        {},
        format="json",
        HTTP_ORIGIN="http://127.0.0.1:5500",
    )
