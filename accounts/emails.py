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
    message = EmailMultiAlternatives(
        "Activate your Videoflix account",
        text_body,
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
    )
    message.attach_alternative(html_body, "text/html")
    return message


def render_activation_bodies(activation_url):
    """Render separate plaintext and autoescaped HTML email bodies."""
    text_context = {"activation_url": mark_safe(activation_url)}
    text_body = render_to_string(
        "accounts/activation_email.txt", text_context
    )
    html_body = render_to_string(
        "accounts/activation_email.html", {"activation_url": activation_url}
    )
    return text_body, html_body


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
