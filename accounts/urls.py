"""URL patterns for account endpoints."""

from django.urls import path

from accounts.views import RegistrationAPIView

urlpatterns = [
    path("register/", RegistrationAPIView.as_view(), name="register"),
]
