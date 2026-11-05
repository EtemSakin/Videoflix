"""Build and send registration activation emails."""

from smtplib import SMTPException
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.utils.safestring import mark_safe


class EmailDeliveryError(Exception):
    """Signal that activation email delivery failed safely."""


def build_activation_url(user):
    """Create the official frontend activation link without logging tokens."""
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    query = urlencode({"uid": uid, "token": token})
    base_url = settings.FRONTEND_BASE_URL.rstrip("/")
    return f"{base_url}/pages/auth/activate.html?{query}"


def build_activation_message(user, activation_url):
    """Render multipart activation content for the recipient."""
    text_body, html_body = render_activation_bodies(activation_url)
    return build_multipart_message(
        "Activate your Videoflix account", user.email, text_body, html_body
    )


def render_activation_bodies(activation_url):
    """Render separate plaintext and autoescaped HTML email bodies."""
    return render_multipart_bodies(
        activation_url,
        "activation_url",
        "accounts/activation_email.txt",
        "accounts/activation_email.html",
    )


def render_multipart_bodies(url, context_key, text_template, html_template):
    """Render plaintext URLs literally and escape them in HTML bodies."""
    text_body = render_to_string(
        text_template, {context_key: mark_safe(url)}
    )
    html_body = render_to_string(html_template, {context_key: url})
    return text_body, html_body


def build_multipart_message(subject, recipient, text_body, html_body):
    """Create one configured multipart email for a recipient."""
    message = EmailMultiAlternatives(
        subject, text_body, settings.DEFAULT_FROM_EMAIL, [recipient]
    )
    message.attach_alternative(html_body, "text/html")
    return message


def send_activation_email(user):
    """Send an activation email or raise a safe delivery error."""
    if not settings.FRONTEND_BASE_URL or not settings.DEFAULT_FROM_EMAIL:
        raise EmailDeliveryError("Email settings are incomplete.")
    message = build_activation_message(user, build_activation_url(user))
    try:
        sent_count = message.send(fail_silently=False)
    except (OSError, SMTPException) as error:
        raise EmailDeliveryError(
            "Activation email delivery failed."
        ) from error
    if sent_count != 1:
        raise EmailDeliveryError("Activation email delivery failed.")


def build_password_reset_url(user):
    """Build the official frontend password-reset link without logging it."""
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    query = urlencode({"uid": uid, "token": token})
    base_url = settings.FRONTEND_BASE_URL.rstrip("/")
    return f"{base_url}/pages/auth/confirm_password.html?{query}"


def build_password_reset_message(user, reset_url):
    """Render a multipart password-reset email for one recipient."""
    text_body, html_body = render_password_reset_bodies(reset_url)
    return build_multipart_message(
        "Reset your Videoflix password", user.email, text_body, html_body
    )


def render_password_reset_bodies(reset_url):
    """Render plaintext and autoescaped HTML password-reset bodies."""
    return render_multipart_bodies(
        reset_url,
        "reset_url",
        "accounts/password_reset_email.txt",
        "accounts/password_reset_email.html",
    )


def send_password_reset_email(user):
    """Send a password-reset link or raise a safe delivery error."""
    if not settings.FRONTEND_BASE_URL or not settings.DEFAULT_FROM_EMAIL:
        raise EmailDeliveryError("Email settings are incomplete.")
    message = build_password_reset_message(
        user, build_password_reset_url(user)
    )
    deliver_password_reset_message(message)


def deliver_password_reset_message(message):
    """Deliver reset mail and normalize SMTP errors for the API layer."""
    try:
        sent_count = message.send(fail_silently=False)
    except (OSError, SMTPException) as error:
        raise EmailDeliveryError(
            "Password reset email delivery failed."
        ) from error
    if sent_count != 1:
        raise EmailDeliveryError("Password reset email delivery failed.")
