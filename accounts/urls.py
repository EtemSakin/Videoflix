"""URL patterns for account endpoints."""

from django.urls import path

from accounts.views import (
    AccountActivationAPIView,
    LoginAPIView,
    LogoutAPIView,
    RegistrationAPIView,
    TokenRefreshAPIView,
)

urlpatterns = [
    path("register/", RegistrationAPIView.as_view(), name="register"),
    path("login/", LoginAPIView.as_view(), name="login"),
    path("logout/", LogoutAPIView.as_view(), name="logout"),
    path(
        "token/refresh/",
        TokenRefreshAPIView.as_view(),
        name="token_refresh",
    ),
    path(
        "activate/<str:uidb64>/<str:token>/",
        AccountActivationAPIView.as_view(),
        name="activate",
    ),
]
