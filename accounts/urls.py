"""URL patterns for account endpoints."""

from django.urls import path

from accounts.views import (
    AccountActivationAPIView,
    LoginAPIView,
    RegistrationAPIView,
)

urlpatterns = [
    path("register/", RegistrationAPIView.as_view(), name="register"),
    path("login/", LoginAPIView.as_view(), name="login"),
    path(
        "activate/<str:uidb64>/<str:token>/",
        AccountActivationAPIView.as_view(),
        name="activate",
    ),
]
